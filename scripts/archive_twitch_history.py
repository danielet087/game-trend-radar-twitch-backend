"""Archive finished raw Twitch day files to GitHub Releases. Nothing is deleted.

Raw day files under data/twitch_history/ are the only complete record of each
hourly census, and they are most of the published site. Every day older than
the newest KEEP_DAYS Taipei days is uploaded here first: one release per month
(tag twitch-history-YYYY-MM), one gzip asset per day, and a manifest with the
SHA-256 of the original JSON bytes. A changed day file is uploaded again. Every
uploaded asset is downloaded back and checked before the manifest records it,
so the manifest only lists days that can be restored byte for byte. An archived
day is replaced only by a file that keeps every archived hour, so a day file
recreated with fewer hours can never shrink its archived copy.

With --frontend the day files come from the frontend checkout until the
Twitch views flag moves them, then from the database-only views source.
"""
from __future__ import annotations

if __package__ in {None, ""}:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
from datetime import date, timedelta
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

KEEP_DAYS = 3
MANIFEST = "twitch-history-manifest.json"
DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def finished_from(days, keep_days: int = KEEP_DAYS) -> list[str]:
    """Days before the newest keep_days Taipei days, which the site will drop."""
    if type(keep_days) is not int or keep_days < 1:
        raise ValueError("keep_days must be a positive integer")
    days = sorted(day for day in days if DAY.fullmatch(day))
    if not days:
        return []
    first_kept = (date.fromisoformat(days[-1]) - timedelta(days=keep_days - 1)).isoformat()
    return [day for day in days if day < first_kept]


def finished_days(history: Path, keep_days: int = KEEP_DAYS) -> list[str]:
    return finished_from((path.stem for path in history.glob("*.json")), keep_days)


def compress(raw: bytes) -> bytes:
    return gzip.compress(raw, compresslevel=9, mtime=0)


class Releases:
    """GitHub Releases through the gh CLI; tests substitute an in-memory store."""

    def __init__(self, repository: str):
        self.repository = repository

    def _gh(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["gh", *args, "--repo", self.repository], check=False,
                              capture_output=True, text=True, timeout=300)

    def ensure(self, tag: str, month: str) -> None:
        if self._gh("release", "view", tag).returncode == 0:
            return
        notes = (f"Twitch 每日原始紀錄（{month}，台灣日期）。每個 .json.gz 解壓後就是原本的 "
                 f"data/twitch_history/<日期>.json；{MANIFEST} 記錄每天原始 JSON 與壓縮檔的 SHA-256。")
        created = self._gh("release", "create", tag, "--title", f"Twitch history {month}", "--notes", notes)
        if created.returncode and self._gh("release", "view", tag).returncode:
            raise RuntimeError(f"Could not create release {tag}")

    def download(self, tag: str, name: str, directory: Path) -> bytes | None:
        result = self._gh("release", "download", tag, "--pattern", name, "--dir", str(directory), "--clobber")
        path = directory / name
        return path.read_bytes() if result.returncode == 0 and path.exists() else None

    def upload(self, tag: str, path: Path) -> None:
        if self._gh("release", "upload", tag, str(path), "--clobber").returncode:
            raise RuntimeError(f"Upload failed: {path.name}")


def _manifest(raw: bytes | None, tag: str) -> dict:
    if raw is None:
        return {"schema_version": 1, "release": tag, "days": {}}
    value = json.loads(raw)
    if value.get("schema_version") != 1 or value.get("release") != tag or not isinstance(value.get("days"), dict):
        raise ValueError(f"Invalid archive manifest in {tag}")
    return value


def _keeps_hours(releases, tag, held, raw, directory) -> bool:
    directory.mkdir(exist_ok=True)
    archived = releases.download(tag, held["asset"], directory)
    if archived is None:
        return True  # Nothing restorable is held; the new copy can only help.
    return set(json.loads(gzip.decompress(archived))["hours"]) <= set(json.loads(raw)["hours"])


def archive(history: Path, releases, *, keep_days: int = KEEP_DAYS) -> dict:
    report = {"archived": [], "unchanged": [], "conflicts": [], "kept_on_site": 0}
    days = finished_days(history, keep_days)
    report["kept_on_site"] = len([p for p in history.glob("*.json") if DAY.fullmatch(p.stem)]) - len(days)
    months: dict[str, list[str]] = {}
    for day in days:
        months.setdefault(day[:7], []).append(day)
    with tempfile.TemporaryDirectory(prefix="twitch-history-archive.") as temporary:
        work = Path(temporary)
        for month, month_days in sorted(months.items()):
            tag = f"twitch-history-{month}"
            releases.ensure(tag, month)
            fetched = work / tag / "fetched"
            fetched.mkdir(parents=True)
            manifest = _manifest(releases.download(tag, MANIFEST, fetched), tag)
            changed = False
            for day in month_days:
                raw = (history / f"{day}.json").read_bytes()
                digest = sha256(raw)
                held = manifest["days"].get(day)
                if held and held.get("json_sha256") == digest:
                    report["unchanged"].append(day)
                    continue
                if held and not _keeps_hours(releases, tag, held, raw, work / tag / "held"):
                    report["conflicts"].append(day)
                    continue
                packed = compress(raw)
                asset = work / tag / f"{day}.json.gz"
                asset.write_bytes(packed)
                releases.upload(tag, asset)
                check = work / tag / "check"
                check.mkdir(exist_ok=True)
                back = releases.download(tag, asset.name, check)
                if back is None or sha256(back) != sha256(packed) or sha256(gzip.decompress(back)) != digest:
                    raise RuntimeError(f"Archived {day} did not verify after upload")
                manifest["days"][day] = {"json_sha256": digest, "json_bytes": len(raw),
                                         "gzip_sha256": sha256(packed), "gzip_bytes": len(packed),
                                         "asset": asset.name}
                report["archived"].append(day)
                changed = True
            if changed:
                manifest["days"] = dict(sorted(manifest["days"].items()))
                path = work / tag / MANIFEST
                path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                releases.upload(tag, path)
    return report


def _archive_frontend(frontend: Path, releases, keep_days: int) -> dict:
    """Read the day files from wherever the Twitch views flag says they live."""
    from radar_backend.state import twitch_views_store as views_store

    if views_store.views_mode(frontend) == "mirror":
        report = archive(frontend / "data/twitch_history", releases, keep_days=keep_days)
        return {**report, "source": "frontend"}
    with tempfile.TemporaryDirectory(prefix="twitch-history-views.") as temporary:
        root = Path(temporary)
        head = views_store.read_into(root, ("data/twitch_history/",))
        report = archive(root / "data/twitch_history", releases, keep_days=keep_days)
    return {**report, "source": "database", "generation": head["generation"]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--history", type=Path, help="data/twitch_history of a frontend checkout")
    where.add_argument("--frontend", type=Path,
                       help="frontend checkout holding data/twitch_history and the Twitch views flag")
    parser.add_argument("--keep-days", type=int, default=KEEP_DAYS)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    args = parser.parse_args(argv)
    if not args.repository:
        raise SystemExit("A GitHub repository for the releases is required")
    releases = Releases(args.repository)
    if args.frontend:
        report = _archive_frontend(args.frontend, releases, args.keep_days)
    else:
        report = archive(args.history, releases, keep_days=args.keep_days)
    summary = (f"Archived {len(report['archived'])} day(s), {len(report['unchanged'])} already archived, "
               f"{report['kept_on_site']} newest day(s) not archived yet.")
    if report.get("source"):
        summary += f" Day files read from the {report['source']} copy."
    if report["conflicts"]:
        summary += f" Kept the archived copy of {', '.join(report['conflicts'])}: the site file lost hours."
        print("::warning::" + summary)
    print(summary)
    print(json.dumps(report, ensure_ascii=False))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
            stream.write("### Twitch 原始紀錄封存\n\n" + summary + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
