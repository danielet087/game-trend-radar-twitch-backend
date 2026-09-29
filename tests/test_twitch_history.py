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


def filtered_audience(**changes):
    return {
        "rule": "followers_gt_1000_viewers_gte_10_v1",
        "min_followers_exclusive": 1000, "min_viewers_inclusive": 10,
        "followers_max_age_hours": 24, "status": "complete",
        "median_viewer_count": 20, "eligible_streamer_count": 2,
        "eligible_viewer_count": 40, "excluded_low_viewer_count": 1,
        "excluded_low_follower_count": 2, "unknown_follower_count": 0,
        **changes,
    }


@pytest.mark.parametrize("audience", [
    filtered_audience(),
    filtered_audience(status="partial", median_viewer_count=None,
                      excluded_low_follower_count=1, unknown_follower_count=1),
    filtered_audience(median_viewer_count=None, eligible_streamer_count=0,
                      eligible_viewer_count=0, excluded_low_follower_count=4),
])
def test_history_preserves_filtered_population_without_rewriting_older_hours(tmp_path, audience):
    relative = store_snapshot(snapshot("2026-09-28T16:07:00Z"), tmp_path)
    new = snapshot("2026-09-28T17:07:00Z")
    new["candidate_games"][0]["filtered_audience"] = audience
    store_snapshot(new, tmp_path)
    hours = json.loads((tmp_path/relative).read_text())["hours"]
    old_row = hours["2026-09-28T16:00:00Z"]["games"][0]
    new_row = hours["2026-09-28T17:00:00Z"]["games"][0]
    assert "filtered_audience" not in old_row
    assert old_row["median_viewer_count"] == new_row["median_viewer_count"] == 300
    assert new_row["filtered_audience"] == audience
    assert new_row["viewer_count"] == 10000
    assert new_row["streamer_count"] == 5


@pytest.mark.parametrize("changes", [
    {"rule": "followers_gte_1000"},
    {"min_followers_exclusive": 999},
    {"min_viewers_inclusive": 9},
    {"followers_max_age_hours": 48},
    {"eligible_streamer_count": True},
    {"excluded_low_follower_count": -1},
    {"unknown_follower_count": 1},
    {"unknown_follower_count": 1, "excluded_low_follower_count": 1},
    {"unknown_follower_count": 1, "excluded_low_follower_count": 1, "status": "partial"},
    {"status": "partial", "median_viewer_count": None},
    {"median_viewer_count": None},
    {"median_viewer_count": 9},
    {"median_viewer_count": float("nan")},
    {"eligible_viewer_count": 19},
    {"eligible_viewer_count": 10001},
    {"eligible_streamer_count": 0, "eligible_viewer_count": 1,
     "excluded_low_follower_count": 4, "median_viewer_count": None},
])
def test_invalid_filtered_metrics_never_replace_last_snapshot(tmp_path, changes):
    store_snapshot(snapshot(), tmp_path)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
    new = snapshot("2026-09-28T17:07:00Z")
    new["candidate_games"][0]["filtered_audience"] = filtered_audience(**changes)
    with pytest.raises(ValueError):
        store_snapshot(new, tmp_path)
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*.json")}


def scheduled_snapshot(start="2026-09-28T16:52:00Z", completed="2026-09-28T17:17:00Z", *,
                       target="2026-09-28T16:00:00Z", run_id="101", viewers=10000):
    payload = snapshot(completed, viewers=viewers)
    payload["collection_started_at"] = start
    payload["collection_schedule"] = {
        "target_slot": target, "trigger_source": "cloudflare", "run_id": run_id, "run_attempt": "1",
    }
    payload["candidate_games"][0]["measurement_started_at"] = start
    return payload


def test_scheduled_cross_hour_records_real_observation_and_separate_completion(tmp_path):
    payload = scheduled_snapshot()
    relative = store_snapshot(payload, tmp_path)
    history = json.loads((tmp_path / relative).read_text())
    assert list(history["hours"]) == ["2026-09-28T16:00:00Z"]
    entry = history["hours"]["2026-09-28T16:00:00Z"]
    assert entry["generated_at"] == "2026-09-28T17:17:00Z"
    assert entry["collection_started_at"] == "2026-09-28T16:52:00Z"
    assert entry["collection_schedule"] == payload["collection_schedule"]
    assert entry["games"][0]["measurement_started_at"] == "2026-09-28T16:52:00Z"
    assert entry["games"][0]["measurement_finished_at"] == "2026-09-28T17:17:00Z"
    receipt = json.loads((tmp_path / "data/twitch_collection_status.json").read_text())
    assert receipt == {
        "schema_version": 1, "observed_slot": "2026-09-28T16:00:00Z",
        "target_slot": "2026-09-28T16:00:00Z", "collection_started_at": "2026-09-28T16:52:00Z",
        "completed_at": "2026-09-28T17:17:00Z", "generated_at": "2026-09-28T17:17:00Z",
        "collection_complete": True, "run_id": "101", "history_path": relative,
    }


def test_scheduled_cross_midnight_uses_start_day_and_never_backfills_requested_slot(tmp_path):
    payload = scheduled_snapshot("2026-09-28T15:52:00Z", "2026-09-28T16:17:00Z", target="2026-09-28T12:00:00Z")
    relative = store_snapshot(payload, tmp_path)
    assert relative == "data/twitch_history/2026-09-28.json"
    history = json.loads((tmp_path / relative).read_text())
    assert list(history["hours"]) == ["2026-09-28T15:00:00Z"]
    assert not (tmp_path / "data/twitch_history/2026-09-29.json").exists()
    receipt = json.loads((tmp_path / "data/twitch_collection_status.json").read_text())
    assert receipt["observed_slot"] == "2026-09-28T15:00:00Z"
    assert receipt["target_slot"] == "2026-09-28T12:00:00Z"


def test_new_scheduler_preserves_legacy_history_bytes_and_legacy_does_not_claim_receipt(tmp_path):
    legacy = snapshot("2026-09-28T15:57:00Z")
    legacy["collection_started_at"] = "2026-09-28T14:52:00Z"
    legacy_relative = store_snapshot(legacy, tmp_path)
    legacy_bytes = (tmp_path / legacy_relative).read_bytes()
    assert not (tmp_path / "data/twitch_collection_status.json").exists()
    new_relative = store_snapshot(scheduled_snapshot(), tmp_path)
    assert new_relative != legacy_relative
    assert (tmp_path / legacy_relative).read_bytes() == legacy_bytes
    assert list(json.loads(legacy_bytes)["hours"]) == ["2026-09-28T15:00:00Z"]


def test_scheduled_idempotency_and_older_sample_cannot_rewind_latest_or_receipt(tmp_path):
    payload = scheduled_snapshot()
    relative = store_snapshot(payload, tmp_path)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
    store_snapshot(deepcopy(payload), tmp_path)
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
    # Even if the old collection finished later, its observations are older.
    stale = scheduled_snapshot("2026-09-28T16:40:00Z", "2026-09-28T17:25:00Z", run_id="99", viewers=12000)
    store_snapshot(stale, tmp_path)
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
    # An older genuine hour can still be archived without claiming currentness.
    older = scheduled_snapshot("2026-09-28T15:42:00Z", "2026-09-28T16:01:00Z", target="2026-09-28T15:00:00Z", run_id="98")
    older_relative = store_snapshot(older, tmp_path)
    assert (tmp_path / older_relative).exists()
    assert (tmp_path / relative).read_bytes() == before[tmp_path / relative]
    for file in ("data/twitch_live.json", "data/twitch_collection_status.json"):
        assert (tmp_path / file).read_bytes() == before[tmp_path / file]


def test_scheduled_empty_complete_census_produces_valid_receipt(tmp_path):
    payload = scheduled_snapshot()
    payload["candidate_games"] = []
    relative = store_snapshot(payload, tmp_path)
    receipt = json.loads((tmp_path / "data/twitch_collection_status.json").read_text())
    assert receipt["collection_complete"] is True
    assert receipt["history_path"] == relative
    history = json.loads((tmp_path / relative).read_text())
    assert history["hours"][receipt["observed_slot"]]["games"] == []


@pytest.mark.parametrize("changes", [
    {"target_slot": "2026-09-28T16:01:00Z"},
    {"target_slot": "2026-09-29T00:00:00+08:00"},
    {"target_slot": "2026-09-28T17:00:00Z"},
    {"target_slot": "2026-09-28T16:00:00"},
    {"target_slot": None},
    {"trigger_source": "unknown"},
    {"run_id": ""}, {"run_id": "abc"}, {"run_id": 101}, {"run_id": "0"},
    {"run_attempt": "-1"}, {"run_attempt": "1.0"}, {"run_attempt": "１"},
])
def test_invalid_schedule_metadata_does_not_change_any_file(tmp_path, changes):
    store_snapshot(scheduled_snapshot(), tmp_path)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
    payload = scheduled_snapshot("2026-09-28T16:53:00Z", "2026-09-28T17:18:00Z")
    payload["collection_schedule"].update(changes)
    with pytest.raises(ValueError):
        store_snapshot(payload, tmp_path)
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*.json")}


@pytest.mark.parametrize("field,value", [
    ("collection_schedule", None),
    ("collection_started_at", "2026-09-28T18:00:00Z"),
    ("collection_started_at", "2026-09-28T16:52:00"),
    ("generated_at", "invalid"),
    ("generated_at", "2026-09-28T15:00:00Z"),
])
def test_invalid_scheduled_timestamps_do_not_create_files(tmp_path, field, value):
    payload = scheduled_snapshot()
    payload[field] = value
    with pytest.raises(ValueError):
        store_snapshot(payload, tmp_path)
    assert not list(tmp_path.rglob("*.json"))


@pytest.mark.parametrize("field,value", [
    ("measurement_started_at", "2026-09-28T16:51:00Z"),
    ("measurement_finished_at", "2026-09-28T17:18:00Z"),
    ("measurement_finished_at", "2026-09-28T16:51:00Z"),
])
def test_scheduled_measurements_must_stay_inside_recorded_collection_window(tmp_path, field, value):
    payload = scheduled_snapshot()
    payload["candidate_games"][0][field] = value
    with pytest.raises(ValueError):
        store_snapshot(payload, tmp_path)
    assert not list(tmp_path.rglob("*.json"))


def test_receipt_can_be_restored_when_exact_snapshot_is_already_published(tmp_path):
    payload = scheduled_snapshot()
    store_snapshot(payload, tmp_path)
    status_path = tmp_path / "data/twitch_collection_status.json"
    receipt = status_path.read_bytes()
    status_path.unlink()
    store_snapshot(payload, tmp_path)
    assert status_path.read_bytes() == receipt


def test_malformed_existing_receipt_blocks_partial_writes(tmp_path):
    store_snapshot(scheduled_snapshot(), tmp_path)
    status_path = tmp_path / "data/twitch_collection_status.json"
    receipt = json.loads(status_path.read_text())
    receipt["observed_slot"] = "2026-09-28T12:00:00Z"
    status_path.write_text(json.dumps(receipt))
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
    with pytest.raises(ValueError):
        store_snapshot(scheduled_snapshot("2026-09-28T18:01:00Z", "2026-09-28T18:20:00Z", target="2026-09-28T18:00:00Z"), tmp_path)
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
