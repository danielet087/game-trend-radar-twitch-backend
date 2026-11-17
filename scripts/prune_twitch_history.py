"""Remove archived raw Twitch day files from the site through DB-first publication.

Only days older than the newest KEEP_DAYS Taipei days leave the site, and each
one only when all of these hold:

1. its closing view exists, so the stream-planning page can still replay it;
2. its monthly release manifest records the file's exact SHA-256;
3. the archived asset downloads and verifies against the manifest.

Any other day stays and is reported. The 24-hour chart reads only the newest
days, series keep the long history, and the archive keeps every raw byte.

Once the Twitch views flag in the frontend source moves the views to the
database, the same rules run against the database-only views source and the
removal is a compare-and-swap on the generation that was checked.
"""
from __future__ import annotations

if __package__ in {None, ""}:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import gzip
import json
import os
from pathlib import Path
import subprocess
import tempfile

from scripts.archive_twitch_history import (
    KEEP_DAYS, MANIFEST, Releases, _manifest, finished_days, finished_from, sha256,
)
from radar_backend.state.twitch_history_views import CLOSE_PATH, HISTORY_PATH, closing_hour


class Verifier:
    """Checks each day against its archive once per file content."""

    def __init__(self, releases, work: Path):
        self.releases, self.work = releases, work
        self.manifests: dict[str, dict | None] = {}
        self.verified: set[tuple[str, str]] = set()

    def _manifest(self, tag):
        if tag not in self.manifests:
            directory = self.work / tag
            directory.mkdir(parents=True, exist_ok=True)
            raw = self.releases.download(tag, MANIFEST, directory)
            try:
                self.manifests[tag] = _manifest(raw, tag) if raw is not None else None
            except ValueError:
                self.manifests[tag] = None
        return self.manifests[tag]

    def blocker(self, frontend: Path, day: str) -> str | None:
        """Why the day must stay on the site, or None when it may leave."""
        close = frontend / CLOSE_PATH / f"{day}.json"
        try:
            view = json.loads(close.read_text(encoding="utf-8")) if close.exists() else None
        except ValueError:
            view = None
        if not (isinstance(view, dict) and view.get("date") == day and isinstance(view.get("hours"), dict)
                and closing_hour(view) is not None):
            return "no usable closing view"
        digest = sha256((frontend / HISTORY_PATH / f"{day}.json").read_bytes())
        if (day, digest) in self.verified:
            return None
        tag = f"twitch-history-{day[:7]}"
        manifest = self._manifest(tag)
        held = (manifest or {}).get("days", {}).get(day)
        if not held:
            return "not archived"
        if held.get("json_sha256") != digest:
            return "site file differs from the archive"
        asset = self.releases.download(tag, held["asset"], self.work / tag)
        if asset is None or sha256(asset) != held.get("gzip_sha256") or sha256(gzip.decompress(asset)) != digest:
            return "archived copy did not verify"
        self.verified.add((day, digest))
        return None


def plan(frontend: Path, verifier: Verifier, keep_days: int = KEEP_DAYS) -> dict:
    history = frontend / HISTORY_PATH
    report = {"remove": [], "kept": {}, "remove_bytes": 0}
    for day in finished_days(history, keep_days):
        reason = verifier.blocker(frontend, day)
        if reason:
            report["kept"][day] = reason
        else:
            report["remove"].append(day)
            report["remove_bytes"] += (history / f"{day}.json").stat().st_size
    return report


def prune(frontend: Path, verifier: Verifier, keep_days: int = KEEP_DAYS) -> dict:
    report = plan(frontend, verifier, keep_days)
    for day in report["remove"]:
        (frontend / HISTORY_PATH / f"{day}.json").unlink()
    return report


def prune_database(verifier: Verifier, keep_days: int = KEEP_DAYS, *, apply: bool,
                   payload_revision: str | None, work: Path) -> dict:
    """Apply the same rules to the database-only views source."""
    from radar_core.storage.sql import StorageError
    from radar_backend.state import twitch_views_store as views_store

    for attempt in range(1, views_store.CAS_ATTEMPTS + 1):
        head, _ = views_store.read(())
        days = [path[len(HISTORY_PATH):-len(".json")] for path in head["manifest"]
                if path.startswith(HISTORY_PATH) and path.endswith(".json")]
        closes = [f"{CLOSE_PATH}{day}.json" for day in finished_from(days, keep_days)]
        root = work / f"views-{attempt}"
        head = views_store.read_into(root, (HISTORY_PATH, *closes))
        (root / HISTORY_PATH).mkdir(parents=True, exist_ok=True)
        report = {**plan(root, verifier, keep_days), "source": "database", "generation": head["generation"]}
        if not apply or not report["remove"]:
            return report
        try:
            result = views_store.delete([f"{HISTORY_PATH}{day}.json" for day in report["remove"]],
                                        base_generation=head["generation"], payload_revision=payload_revision)
        except StorageError as error:
            if error.code == "STALE_GENERATION" and attempt < views_store.CAS_ATTEMPTS:
                continue  # Re-check against the newer generation.
            raise
        return {**report, "generation": result["generation"]}
    raise StorageError("STALE_GENERATION", "The views source kept changing during the removal.")


def _revision() -> str:
    from radar_core.publication import snapshot_revision
    return snapshot_revision({"schema": 1, "code": os.environ["GITHUB_SHA"],
                              "run": os.environ["GITHUB_RUN_ID"],
                              "attempt": os.environ.get("GITHUB_RUN_ATTEMPT", "1")})


def _summary(report, applied):
    verb = "Removed" if applied else "Would remove"
    text = f"{verb} {len(report['remove'])} archived day file(s), {report['remove_bytes'] / 1e6:.1f} MB."
    if report["kept"]:
        text += " Kept: " + ", ".join(f"{day} ({reason})" for day, reason in report["kept"].items()) + "."
    return text


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-days", type=int, default=KEEP_DAYS)
    parser.add_argument("--apply", action="store_true", help="Publish the removal; otherwise only report.")
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    args = parser.parse_args(argv)
    if not args.repository:
        raise SystemExit("A GitHub repository for the releases is required")
    from radar_core.publication import SubprocessGitRepository, publish_with_retry
    from radar_backend.jobs.publish_twitch import FRONTEND_URL, ROOT
    from radar_backend.state import twitch_views_store as views_store

    reports = []
    with tempfile.TemporaryDirectory(prefix="radar-twitch-prune.") as temporary:
        verifier = Verifier(Releases(args.repository), Path(temporary) / "archive")
        if views_store.mode_in_database() == "primary":
            reports.append(prune_database(verifier, args.keep_days, apply=args.apply,
                                          payload_revision=_revision() if args.apply else None,
                                          work=Path(temporary)))
            return _finish(reports[-1], args.apply)
        frontend = Path(temporary) / "frontend"
        subprocess.run(["git", "clone", "--depth", "1", "--branch", "main", FRONTEND_URL, str(frontend)], check=True)
        if not args.apply:
            reports.append(plan(frontend, verifier, args.keep_days))
        else:
            if not os.environ.get("FRONTEND_REPO_TOKEN", "").strip():
                raise SystemExit("FRONTEND_REPO_TOKEN is not configured")
            for key, value in (("user.name", "github-actions[bot]"),
                               ("user.email", "41898282+github-actions[bot]@users.noreply.github.com")):
                subprocess.run(["git", "-C", str(frontend), "config", key, value], check=True)
            revision = _revision()
            repository = SubprocessGitRepository(frontend, disposable_checkout=True, environment=os.environ,
                push_command_prefix=("bash", str(ROOT / "scripts/git_frontend_auth.sh")))
            # The callback runs on the database snapshot and again on every retry.
            receipt = publish_with_retry(repository, lambda root: reports.append(prune(root, verifier, args.keep_days)),
                paths=(HISTORY_PATH,), message="data: remove archived Twitch day files from the site",
                input_revision=revision, payload_revision=revision)
            print(f"Published at frontend {receipt.published_revision} (changed={receipt.changed}).")
    return _finish(reports[-1], args.apply)


def _finish(report: dict, applied: bool) -> int:
    summary = _summary(report, applied)
    if report.get("source") == "database":
        summary += f" Database views source generation {report['generation']}."
    print(summary)
    print(json.dumps(report, ensure_ascii=False))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
            stream.write("### Twitch 原始日檔移除\n\n" + summary + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
