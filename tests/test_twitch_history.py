from copy import deepcopy
import json

import pytest

from scripts.store_twitch_snapshot import store_snapshot


def snapshot(at="2026-09-28T16:30:00Z", status="pending", viewers=10000):
    row = {"game_id": "1", "game_name": "Example", "viewer_count": viewers, "streamer_count": 5,
           "median_viewer_count": 300, "pagination_complete": True,
           "measurement_started_at": at, "measurement_finished_at": at,
           "verification": {"status": status}}
    return {"schema_version": 2, "generated_at": at, "collection_started_at": at,
            "min_viewers": 7000, "coverage": {"collection_complete": True, "stop_reason": "category_directory_exhausted"},
            "candidate_games": [row], "top_games": [row] if status == "new" else [], "tracked_games": []}


def test_taipei_daily_history_retains_pending_and_preserves_prior_hours(tmp_path):
    path = store_snapshot(snapshot("2026-09-28T16:30:00Z"), tmp_path)
    assert path == "data/twitch_history/2026-09-29.json"
    store_snapshot(snapshot("2026-09-28T17:30:00Z", "new"), tmp_path)
    history = json.loads((tmp_path/path).read_text())
    assert len(history["hours"]) == 2
    assert history["hours"]["2026-09-28T16:00:00Z"]["games"][0]["verification"]["status"] == "pending"
    assert history["hours"]["2026-09-28T17:00:00Z"]["games"][0]["verification"]["status"] == "new"


def test_hourly_rerun_is_idempotent_and_late_runs_do_not_rewind_latest(tmp_path):
    path = store_snapshot(snapshot(), tmp_path)
    files_before = {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
    store_snapshot(snapshot(), tmp_path)
    assert files_before == {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
    store_snapshot(snapshot("2026-09-28T16:40:00Z", viewers=12000), tmp_path)
    store_snapshot(snapshot("2026-09-28T16:10:00Z", viewers=8000), tmp_path)
    history = json.loads((tmp_path/path).read_text())
    assert len(history["hours"]) == 1
    assert history["hours"]["2026-09-28T16:00:00Z"]["games"][0]["viewer_count"] == 12000
    assert json.loads((tmp_path/"data/twitch_live.json").read_text())["generated_at"] == "2026-09-28T16:40:00Z"
    store_snapshot(snapshot("2026-09-28T15:20:00Z"), tmp_path)
    assert (tmp_path/"data/twitch_history/2026-09-28.json").exists()
    assert json.loads((tmp_path/"data/twitch_live.json").read_text())["generated_at"] == "2026-09-28T16:40:00Z"


@pytest.mark.parametrize("field,value", [("median_viewer_count", None), ("median_viewer_count", float("nan")), ("viewer_count", 6999),
                                        ("streamer_count", 0), ("pagination_complete", False), ("verification", {"status": "not_new"})])
def test_invalid_snapshot_does_not_create_files(tmp_path, field, value):
    payload = snapshot()
    payload["candidate_games"][0][field] = value
    with pytest.raises(ValueError):
        store_snapshot(payload, tmp_path)
    assert not list(tmp_path.rglob("*.json"))


def test_partial_scan_does_not_overwrite_latest(tmp_path):
    payload = snapshot()
    store_snapshot(payload, tmp_path)
    before = (tmp_path/"data/twitch_live.json").read_bytes()
    bad = deepcopy(payload)
    bad["coverage"]["collection_complete"] = False
    with pytest.raises(ValueError):
        store_snapshot(bad, tmp_path)
    assert (tmp_path/"data/twitch_live.json").read_bytes() == before
