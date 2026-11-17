"""The database-only home of the high-churn Twitch views.

Raw day files, 24-hour compact views, closing views and per-game series move
out of the frontend's Git history into the storage source
``danielet087/game-trend-radar@twitch-views``, which is never projected to Git;
the site deploy adds the files back. ``RADAR_TWITCH_VIEWS`` selects the stage:

* ``mirror`` (default): the frontend source stays authoritative. After each
  merge the views are copied to the database source; a failure only warns,
  because the next hour copies every difference again.
* ``primary``: the database source is authoritative. Views are materialized
  from it before the merge, written back afterwards (a failure stops the
  publication) and removed from the frontend checkout, so the first such run
  deletes them from the frontend source and its Git projection.

Only names, digests and byte counts are logged; never credentials.
"""
from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
from typing import Mapping

from radar_core.publication import snapshot_revision
from radar_core.storage import primary
from radar_core.storage.sql import StorageError

from radar_backend.state.twitch_history_views import CLOSE_PATH, COMPACT_PATH, HISTORY_PATH
from radar_backend.state.twitch_series import SERIES_PATH

REPOSITORY = "danielet087/game-trend-radar"
BRANCH = "twitch-views"
PREFIXES = (HISTORY_PATH, COMPACT_PATH, CLOSE_PATH, SERIES_PATH)
MODES = ("mirror", "primary")
CAS_ATTEMPTS = 3


def views_mode(environment: Mapping[str, str]) -> str:
    value = environment.get("RADAR_TWITCH_VIEWS", "mirror").strip() or "mirror"
    if value not in MODES:
        raise ValueError("RADAR_TWITCH_VIEWS must be mirror or primary")
    return value


def is_view(path: str) -> bool:
    return path.endswith(".json") and path.startswith(PREFIXES)


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def local_views(root: Path) -> dict[str, bytes]:
    files = {}
    for prefix in PREFIXES:
        base = root / prefix
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.json")):
            if path.is_symlink() or not path.is_file():
                raise ValueError("Twitch views cannot use symlinks")
            relative = path.relative_to(root).as_posix()
            if PurePosixPath(relative).as_posix() != relative:
                raise ValueError("Unsafe Twitch view path")
            files[relative] = path.read_bytes()
    return files


def _clear_local(root: Path) -> None:
    for path in local_views(root):
        (root / path).unlink()


def materialize(root: Path) -> dict:
    """Replace the checkout's views with the authoritative database copy."""
    known = local_views(root)
    head, files = primary.read_files(REPOSITORY, paths=PREFIXES, branch=BRANCH, known_files=known)
    _clear_local(root)
    for path, raw in files.items():
        if not is_view(path):
            raise StorageError("INVALID_PATH", "The views source holds a document outside its directories.")
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    return {"generation": head["generation"], "files": len(files)}


def sync(root: Path, *, payload_revision: str) -> dict:
    """Write every local difference to the database copy (compare-and-swap)."""
    local = local_views(root)
    for attempt in range(1, CAS_ATTEMPTS + 1):
        head, _ = primary.read_files(REPOSITORY, paths=(), branch=BRANCH)
        manifest = {path: entry for path, entry in head["manifest"].items() if is_view(path)}
        changed = {path: raw for path, raw in local.items()
                   if manifest.get(path, {}).get("digest") != _digest(raw)}
        deleted = sorted(path for path in manifest if path not in local)
        if not changed and not deleted:
            return {"status": "unchanged", "generation": head["generation"], "files": len(local)}
        operation_id = snapshot_revision({
            "payload_revision": payload_revision, "deleted": deleted,
            "changed": {path: _digest(raw) for path, raw in sorted(changed.items())},
        })
        try:
            receipt = primary.write_files(
                REPOSITORY, changed, base_generation=head["generation"], deleted=deleted,
                operation_id=operation_id, payload_revision=payload_revision,
                branch=BRANCH, projection_required=False,
            )
        except StorageError as error:
            if error.code == "STALE_GENERATION" and attempt < CAS_ATTEMPTS:
                continue
            raise
        return {"status": "written", "generation": receipt.get("generation"), "changed": len(changed),
                "deleted": len(deleted), "files": len(local), "attempts": attempt}
    raise StorageError("STALE_GENERATION", "The views source kept changing during the write.")


def remove_local(root: Path) -> int:
    """After a primary write, keep the views out of the frontend commit."""
    count = len(local_views(root))
    _clear_local(root)
    for prefix in PREFIXES:
        base = root / prefix
        if not base.is_dir():
            continue
        for directory in [*sorted((p for p in base.rglob("*") if p.is_dir()), reverse=True), base]:
            if not any(directory.iterdir()):
                directory.rmdir()
    return count
