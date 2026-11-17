"""The database-only home of the high-churn Twitch views.

Raw day files, 24-hour compact views, closing views and per-game series move
out of the frontend's Git history into the storage source
``danielet087/game-trend-radar@twitch-views``, which is never projected to Git;
the site deploy adds the files back. The flag file
``data/publication/twitch_views.json`` in the frontend source says which stage
applies, so every job follows the same switch:

* mirror (no flag): the frontend source stays authoritative. After each merge
  the views are copied to the database source; a failure only warns, because
  the next hour copies every difference again.
* primary (flag ``"home": "database"``): the database source is authoritative.
  Views are materialized from it before the merge, written back with a
  compare-and-swap on the generation that was read (a failure stops the
  publication) and kept out of the frontend commit.

Only ``scripts/move_twitch_views.py`` writes the flag, in the same frontend
publication that removes the four directories, after checking that both copies
are identical. A stale writer that re-adds views to the frontend cannot remove
the flag, and the next primary run drops what it added.

Only names, digests and byte counts are logged; never credentials.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Iterable

from radar_core.publication import snapshot_revision
from radar_core.storage import primary
from radar_core.storage.sql import StorageError

from radar_backend.state.twitch_history_views import CLOSE_PATH, COMPACT_PATH, HISTORY_PATH
from radar_backend.state.twitch_series import SERIES_PATH

REPOSITORY = "danielet087/game-trend-radar"
BRANCH = "twitch-views"
FRONTEND_BRANCH = "main"
SOURCE = f"{REPOSITORY}@{BRANCH}"
PREFIXES = (HISTORY_PATH, COMPACT_PATH, CLOSE_PATH, SERIES_PATH)
FLAG = "data/publication/twitch_views.json"
INDEX = SERIES_PATH + "index.json"
MODES = ("mirror", "primary")
CAS_ATTEMPTS = 3


def flag_mode(raw: bytes | None) -> str:
    """primary when the flag moves the views to the database, else mirror."""
    if raw is None:
        return "mirror"
    try:
        value = json.loads(raw)
    except ValueError:
        value = None
    if not (isinstance(value, dict) and value.get("schema_version") == 1
            and value.get("home") == "database" and value.get("source") == SOURCE):
        raise ValueError(f"{FLAG} is not a valid Twitch views flag")
    return "primary"


def views_mode(root: Path) -> str:
    """The stage recorded in a frontend checkout or publication snapshot."""
    path = root / FLAG
    return flag_mode(path.read_bytes() if path.is_file() else None)


def mode_in_database() -> str:
    """The stage recorded in the frontend source itself."""
    _, files = primary.read_files(REPOSITORY, paths=(FLAG,), branch=FRONTEND_BRANCH)
    return flag_mode(files.get(FLAG))


def flag_document(*, generation: int, files: int, raw_bytes: int, moved_at: str) -> bytes:
    value = {"schema_version": 1, "home": "database", "source": SOURCE,
             "directories": list(PREFIXES), "generation": generation, "files": files,
             "bytes": raw_bytes, "moved_at": moved_at}
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


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


def differences(local: dict[str, bytes], manifest: dict) -> dict[str, list[str]]:
    """Compare checkout bytes with a views-source manifest, without downloading."""
    held = {path: entry for path, entry in manifest.items() if is_view(path)}
    return {
        "only_frontend": sorted(set(local) - set(held)),
        "only_database": sorted(set(held) - set(local)),
        "different": sorted(path for path in set(local) & set(held)
                            if held[path].get("digest") != _digest(local[path])),
    }


def _clear_local(root: Path) -> None:
    for path in local_views(root):
        (root / path).unlink()


def read(paths: Iterable[str]) -> tuple[dict, dict[str, bytes]]:
    """One consistent read of the views source; ``()`` returns only the head."""
    head, files = primary.read_files(REPOSITORY, paths=tuple(paths), branch=BRANCH)
    if any(not is_view(path) for path in files):
        raise StorageError("INVALID_PATH", "The views source holds a document outside its directories.")
    return head, files


def read_into(root: Path, paths: Iterable[str]) -> dict:
    """Write the selected views under root and return the head they came from."""
    head, files = read(paths)
    for path, raw in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    return head


def materialize(root: Path) -> dict:
    """Replace the checkout's views with the authoritative database copy."""
    known = local_views(root)
    head, files = primary.read_files(REPOSITORY, paths=PREFIXES, branch=BRANCH, known_files=known)
    if any(not is_view(path) for path in files):
        raise StorageError("INVALID_PATH", "The views source holds a document outside its directories.")
    if INDEX not in files:
        # An empty source would let a merge start a new, tiny history.
        raise StorageError("MISSING_VIEWS", "The views source has no series index.")
    _clear_local(root)
    for path, raw in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    return {"generation": head["generation"], "files": len(files)}


def _write(changed: dict[str, bytes], deleted: list[str], *, base_generation: int,
           payload_revision: str) -> dict:
    operation_id = snapshot_revision({
        "payload_revision": payload_revision, "deleted": deleted,
        "changed": {path: _digest(raw) for path, raw in sorted(changed.items())},
    })
    return primary.write_files(
        REPOSITORY, changed, base_generation=base_generation, deleted=deleted,
        operation_id=operation_id, payload_revision=payload_revision,
        branch=BRANCH, projection_required=False,
    )


def sync(root: Path, *, payload_revision: str, base_generation: int | None = None) -> dict:
    """Write every local difference to the database copy (compare-and-swap).

    With ``base_generation`` (primary mode) the local views were materialized
    from that generation, so any newer generation stops the write instead of
    being overwritten. Without it (mirror mode) the frontend copy wins and a
    concurrent writer only causes a retry.
    """
    local = local_views(root)
    attempts = 1 if base_generation is not None else CAS_ATTEMPTS
    for attempt in range(1, attempts + 1):
        head, _ = read(())
        if base_generation is not None and head["generation"] != base_generation:
            raise StorageError("STALE_GENERATION", "The views source changed after it was materialized.")
        manifest = {path: entry for path, entry in head["manifest"].items() if is_view(path)}
        changed = {path: raw for path, raw in local.items()
                   if manifest.get(path, {}).get("digest") != _digest(raw)}
        deleted = sorted(path for path in manifest if path not in local)
        if not changed and not deleted:
            return {"status": "unchanged", "generation": head["generation"], "files": len(local)}
        try:
            receipt = _write(changed, deleted, base_generation=head["generation"],
                             payload_revision=payload_revision)
        except StorageError as error:
            if error.code == "STALE_GENERATION" and attempt < attempts:
                continue
            raise
        return {"status": "written", "generation": receipt.get("generation"), "changed": len(changed),
                "deleted": len(deleted), "files": len(local), "attempts": attempt}
    raise StorageError("STALE_GENERATION", "The views source kept changing during the write.")


def delete(paths: Iterable[str], *, base_generation: int, payload_revision: str) -> dict:
    """Remove views from the database copy if it is still at base_generation."""
    deleted = sorted(set(paths))
    if not deleted or any(not is_view(path) for path in deleted):
        raise ValueError("Only Twitch views can be deleted from the views source")
    receipt = _write({}, deleted, base_generation=base_generation, payload_revision=payload_revision)
    return {"status": "written", "generation": receipt.get("generation"), "deleted": len(deleted)}


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
