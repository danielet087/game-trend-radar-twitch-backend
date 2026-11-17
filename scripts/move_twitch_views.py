"""Move the Twitch views out of the frontend source into the database-only views source.

Without --apply nothing changes: the frontend source's views are read from the
database, compared file by file with the views source, and the read time is
reported (it is what every primary-mode run will spend reading the views).

With --apply, one frontend publication on the Twitch publication lane:

1. copies every remaining difference to the views source (until the move the
   frontend copy is authoritative, so it wins),
2. checks that both copies are identical, file by file,
3. records data/publication/twitch_views.json with "home": "database", and
4. removes the four directories from the frontend source and its Git tip.

Git history keeps every earlier version; nothing is rewritten. Running it again
after the move only drops views that a stale writer put back.
"""
from __future__ import annotations

if __package__ in {None, ""}:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from radar_backend.state import twitch_views_store as views_store


def _counts(differences: dict) -> dict:
    return {key: len(paths) for key, paths in differences.items()}


def compare_in_database() -> dict:
    """Dry run: both copies straight from the database, nothing written."""
    from radar_core.storage import primary

    started = time.monotonic()
    head, files = primary.read_files(views_store.REPOSITORY, paths=(*views_store.PREFIXES, views_store.FLAG),
                                     branch=views_store.FRONTEND_BRANCH)
    seconds = round(time.monotonic() - started, 1)
    mode = views_store.flag_mode(files.pop(views_store.FLAG, None))
    views_head, _ = views_store.read(())
    differences = views_store.differences(files, views_head["manifest"])
    return {"status": "compared", "mode": mode, "frontend_generation": head["generation"],
            "views_generation": views_head["generation"], "frontend_files": len(files),
            "frontend_bytes": sum(map(len, files.values())), "read_seconds": seconds,
            "views_files": sum(1 for path in views_head["manifest"] if views_store.is_view(path)),
            **_counts(differences),
            "examples": {key: paths[:10] for key, paths in differences.items() if paths}}


def cutover(root: Path, *, payload_revision: str, reports: list, now=None) -> None:
    """Publication callback; runs on the frontend database snapshot, again on every retry."""
    if views_store.views_mode(root) == "primary":
        reports.append({"status": "already_moved", "removed_stale_views": views_store.remove_local(root)})
        return
    local = views_store.local_views(root)
    if views_store.INDEX not in local:
        raise ValueError("The frontend source holds no Twitch series index to move")
    head, _ = views_store.read(())
    before = views_store.differences(local, head["manifest"])
    copied = {"status": "identical"}
    if any(before.values()):
        copied = views_store.sync(root, payload_revision=payload_revision)
        head, _ = views_store.read(())
    after = views_store.differences(local, head["manifest"])
    if any(after.values()):
        raise ValueError("The views source still differs from the frontend copy: "
                         + json.dumps(_counts(after)))
    raw_bytes = sum(map(len, local.values()))
    moved_at = (now or datetime.now(timezone.utc)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    flag = root / views_store.FLAG
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_bytes(views_store.flag_document(generation=head["generation"], files=len(local),
                                               raw_bytes=raw_bytes, moved_at=moved_at))
    removed = views_store.remove_local(root)
    reports.append({"status": "moved", "views_generation": head["generation"], "files": len(local),
                    "bytes": raw_bytes, "differences_before": _counts(before), "copied": copied,
                    "removed_from_frontend": removed})


def _revision() -> str:
    from radar_core.publication import snapshot_revision
    return snapshot_revision({"schema": 1, "code": os.environ["GITHUB_SHA"], "run": os.environ["GITHUB_RUN_ID"],
                              "attempt": os.environ.get("GITHUB_RUN_ATTEMPT", "1"), "job": "move-twitch-views"})


def apply_move() -> dict:
    from radar_core.publication import SubprocessGitRepository, publish_with_retry
    from radar_backend.jobs.publish_twitch import FRONTEND_URL, ROOT

    if not os.environ.get("FRONTEND_REPO_TOKEN", "").strip():
        raise SystemExit("FRONTEND_REPO_TOKEN is not configured")
    revision = _revision()
    reports: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="radar-twitch-move.") as temporary:
        frontend = Path(temporary) / "frontend"
        subprocess.run(["git", "clone", "--depth", "1", "--branch", "main", FRONTEND_URL, str(frontend)], check=True)
        for key, value in (("user.name", "github-actions[bot]"),
                           ("user.email", "41898282+github-actions[bot]@users.noreply.github.com")):
            subprocess.run(["git", "-C", str(frontend), "config", key, value], check=True)
        repository = SubprocessGitRepository(frontend, disposable_checkout=True, environment=os.environ,
            push_command_prefix=("bash", str(ROOT / "scripts/git_frontend_auth.sh")))
        receipt = publish_with_retry(
            repository, lambda root: cutover(root, payload_revision=revision, reports=reports),
            paths=(*views_store.PREFIXES, views_store.FLAG),
            message="data: move the Twitch views to the database source",
            input_revision=revision, payload_revision=revision)
    return {**reports[-1], "published_revision": receipt.published_revision, "changed": receipt.changed,
            "attempts": receipt.attempts}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="Move the views; otherwise only compare.")
    args = parser.parse_args(argv)
    report = apply_move() if args.apply else compare_in_database()
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
            stream.write(f"### Twitch views move ({'apply' if args.apply else 'dry-run'})\n\n```json\n{text}\n```\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
