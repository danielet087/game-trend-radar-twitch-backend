"""A day may leave the site only with a closing view and a verified archive."""
import gzip
import json

from radar_backend.state.twitch_history_views import CLOSE_PATH, HISTORY_PATH, project_history_views
from scripts.archive_twitch_history import archive
from scripts.prune_twitch_history import Verifier, plan, prune
from tests.test_archive_twitch_history import MemoryReleases


def write_day(root, day, viewers=10000):
    path = root / HISTORY_PATH / f"{day}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    hour = f"{day}T01:00:00Z"
    path.write_text(json.dumps({"schema_version": 1, "date": day, "timezone": "Asia/Taipei", "hours": {
        hour: {"generated_at": f"{day}T01:10:00Z", "games": [{"game_id": "1", "viewer_count": viewers}]}}}))


def site(tmp_path, days):
    for day in days:
        write_day(tmp_path, day)
    project_history_views(tmp_path)
    return tmp_path


def test_only_verified_finished_days_with_closing_views_leave(tmp_path):
    days = ["2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06", "2026-11-07", "2026-11-08"]
    root = site(tmp_path, days)
    releases = MemoryReleases()
    archive(root / HISTORY_PATH, releases, keep_days=3)
    # 11-02 changed after archiving, 11-03 lost its closing view, 11-04 has a broken archive copy.
    write_day(root, "2026-11-02", viewers=12345)
    (root / CLOSE_PATH / "2026-11-03.json").unlink()
    releases.assets["twitch-history-2026-11"]["2026-11-04.json.gz"] = gzip.compress(b"{}", mtime=0)
    # 11-05 was never archived.
    manifest = json.loads(releases.assets["twitch-history-2026-11"]["twitch-history-manifest.json"])
    del manifest["days"]["2026-11-05"]
    releases.assets["twitch-history-2026-11"]["twitch-history-manifest.json"] = json.dumps(manifest).encode()

    report = plan(root, Verifier(releases, tmp_path / "work-plan"), keep_days=3)
    assert report["remove"] == ["2026-11-01"]
    assert report["kept"] == {"2026-11-02": "site file differs from the archive",
                              "2026-11-03": "no usable closing view",
                              "2026-11-04": "archived copy did not verify",
                              "2026-11-05": "not archived"}
    assert all((root / HISTORY_PATH / f"{day}.json").exists() for day in days)

    removed = prune(root, Verifier(releases, tmp_path / "work-prune"), keep_days=3)
    assert removed["remove"] == ["2026-11-01"]
    remaining = sorted(path.stem for path in (root / HISTORY_PATH).glob("*.json"))
    assert remaining == days[1:]
    # The newest three days always stay, and the closing view outlives the raw file.
    assert (root / CLOSE_PATH / "2026-11-01.json").exists()


def test_a_missing_release_keeps_every_day(tmp_path):
    root = site(tmp_path, ["2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04"])
    report = prune(root, Verifier(MemoryReleases(), tmp_path / "work"), keep_days=3)
    assert report["remove"] == [] and report["kept"] == {"2026-11-01": "not archived"}
    assert (root / HISTORY_PATH / "2026-11-01.json").exists()
