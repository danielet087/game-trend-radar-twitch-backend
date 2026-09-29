from copy import deepcopy
from datetime import datetime, timedelta, timezone
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


def test_history_preserves_experiment_without_promoting_pending_to_official_new(tmp_path):
    payload = snapshot()
    experiment = {"igdb_first_release_date": {"predicted_new": True, "confirms_twitch_new_badge": False}}
    payload["candidate_games"][0]["release_experiment"] = experiment
    path = store_snapshot(payload, tmp_path)
    row = json.loads((tmp_path/path).read_text())["hours"]["2026-09-28T16:00:00Z"]["games"][0]
    assert row["release_experiment"] == experiment
    assert row["verification"]["status"] == "pending"


def test_scheduled_history_accumulates_beyond_thirty_days_without_pruning(tmp_path):
    # Publishing a new day must preserve old files, including a month boundary.
    start = datetime(2026, 9, 29, 15, 7, tzinfo=timezone.utc)
    saved = {}
    for day in range(35):
        for hour in (0, 1):
            at = (start + timedelta(days=day, hours=hour)).isoformat().replace("+00:00", "Z")
            payload = snapshot(at)
            payload["candidate_games"][0]["median_viewer_count"] = 0 if hour == 0 else 0.5
            relative = store_snapshot(payload, tmp_path)
            saved[at] = relative
    expected_days = set(saved.values())
    assert len(list((tmp_path/"data/twitch_history").glob("*.json"))) == len(expected_days)
    for at, relative in saved.items():
        archive = json.loads((tmp_path/relative).read_text())
        key = at[:13] + ":00:00Z"
        entry = archive["hours"][key]
        assert entry["generated_at"] == at
        assert entry["games"][0]["median_viewer_count"] == (0 if "T15:" in at else 0.5)
        assert entry["games"][0]["verification"]["status"] == "pending"
    assert json.loads((tmp_path/"data/twitch_live.json").read_text())["generated_at"] == max(saved)


def test_game_leaving_candidates_keeps_earlier_history_without_zero_fill(tmp_path):
    path = store_snapshot(snapshot("2026-09-28T16:07:00Z"), tmp_path)
    empty = snapshot("2026-09-28T18:07:00Z")
    empty["candidate_games"] = []
    store_snapshot(empty, tmp_path)
    hours = json.loads((tmp_path/path).read_text())["hours"]
    assert hours["2026-09-28T16:00:00Z"]["games"][0]["viewer_count"] == 10000
    assert "2026-09-28T17:00:00Z" not in hours
    assert hours["2026-09-28T18:00:00Z"]["games"] == []
