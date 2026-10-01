from copy import deepcopy
import io
import json
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from scripts import load_twitch_tracking as loader
from scripts.store_twitch_snapshot import merge_tracking_state, store_snapshot


def tracking_state(game_id="1", at="2026-09-28T17:17:00Z", *, first="2026-09-27T16:00:00Z", status="active"):
    return {"schema_version": 1, "updated_at": at, "games": {game_id: {
        "game_id": game_id, "game_name": f"Game {game_id}", "status": status,
        "status_reason": "within_release_window", "first_seen_at": first,
        "last_seen_at": at, "updated_at": at, "release_at": "2026-09-20T00:00:00Z",
        "release_source": "igdb_first_release_date", "expires_at": "2026-10-20T00:00:00Z",
        "enrollment": {"source": "igdb_first_release_date", "observed_at": first,
                       "viewer_count": 8000, "min_viewers": 7000},
    }}}


def tracked_snapshot(viewers=100, streamers=2, middle=50):
    start, completed = "2026-09-28T16:52:00Z", "2026-09-28T17:17:00Z"
    row = {"game_id": "1", "game_name": "Game 1", "viewer_count": viewers,
           "streamer_count": streamers, "median_viewer_count": middle,
           "pagination_complete": True, "verification": {"status": "pending"},
           "measurement_started_at": start, "measurement_finished_at": completed,
           "observation_status": "current", "observation_at": completed}
    state = tracking_state()
    state["games"]["1"]["last_observation"] = deepcopy(row)
    return {"schema_version": 2, "generated_at": completed, "collection_started_at": start,
            "min_viewers": 7000, "coverage": {"collection_complete": True, "stop_reason": "category_directory_exhausted"},
            "collection_schedule": {"target_slot": "2026-09-28T16:00:00Z", "trigger_source": "cloudflare",
                                    "run_id": "101", "run_attempt": "1"},
            "candidate_games": [], "top_games": [], "tracked_games": [row], "tracking_state": state}


@pytest.mark.parametrize("metrics", [(100, 2, 50), (0, 0, None)])
def test_enrolled_below_threshold_and_genuine_empty_census_are_saved(tmp_path, metrics):
    payload = tracked_snapshot(*metrics)
    relative = store_snapshot(payload, tmp_path)
    saved = json.loads((tmp_path / relative).read_text())["hours"]["2026-09-28T16:00:00Z"]["games"]
    assert len(saved) == 1
    assert (saved[0]["viewer_count"], saved[0]["streamer_count"], saved[0]["median_viewer_count"]) == metrics
    assert saved[0]["observation_status"] == "current"
    saved_tracking = json.loads((tmp_path / "data/twitch_tracking.json").read_text())
    assert saved_tracking == loader.validate_persisted_tracking(payload["tracking_state"])
    assert saved_tracking["games"]["1"]["tracking_sources"]["twitch_new"]["enrollment"] == payload["tracking_state"]["games"]["1"]["enrollment"]
    assert (tmp_path / "data/twitch_collection_status.json").exists()


def test_tracked_game_is_retained_after_badge_becomes_not_new(tmp_path):
    payload = tracked_snapshot()
    payload["tracked_games"][0]["verification"]["status"] = "not_new"
    relative = store_snapshot(payload, tmp_path)
    assert len(json.loads((tmp_path / relative).read_text())["hours"]["2026-09-28T16:00:00Z"]["games"]) == 1


def test_candidate_and_tracked_game_are_archived_once(tmp_path):
    payload = tracked_snapshot(10000, 2, 5000)
    payload["candidate_games"] = deepcopy(payload["tracked_games"])
    relative = store_snapshot(payload, tmp_path)
    assert len(json.loads((tmp_path / relative).read_text())["hours"]["2026-09-28T16:00:00Z"]["games"]) == 1


def test_conflicting_candidate_and_tracked_observations_fail_before_writes(tmp_path):
    payload = tracked_snapshot(10000, 2, 5000)
    payload["candidate_games"] = deepcopy(payload["tracked_games"])
    payload["candidate_games"][0]["viewer_count"] = 12000
    with pytest.raises(ValueError, match="disagree"):
        store_snapshot(payload, tmp_path)
    assert not list(tmp_path.rglob("*.json"))


@pytest.mark.parametrize("change", [
    {"observation_status": "stale"}, {"pagination_complete": False},
    {"streamer_count": 0}, {"viewer_count": 0, "streamer_count": 0, "median_viewer_count": 0},
    {"viewer_count": None}, {"median_viewer_count": None},
    {"measurement_finished_at": "2026-09-28T18:00:00Z"},
])
def test_partial_or_retained_observation_never_creates_current_history(tmp_path, change):
    payload = tracked_snapshot()
    payload["tracked_games"][0].update(change)
    with pytest.raises(ValueError):
        store_snapshot(payload, tmp_path)
    assert not list(tmp_path.rglob("*.json"))


def test_stale_publish_unions_games_without_rewinding_enrollment_or_observation():
    newer = tracking_state(at="2026-09-29T17:17:00Z")
    newer["games"]["1"]["last_observation"] = {"game_id": "1", "viewer_count": 10,
                                                  "observation_at": newer["updated_at"]}
    stale = tracking_state(at="2026-09-28T17:17:00Z", first="2026-09-26T00:00:00Z")
    stale["games"]["1"]["last_observation"] = {"game_id": "1", "viewer_count": 999,
                                                  "observation_at": stale["updated_at"]}
    stale["games"].update(tracking_state("2")["games"])
    result = merge_tracking_state(newer, stale)
    assert set(result["games"]) == {"1", "2"}
    assert result["updated_at"] == newer["updated_at"]
    row = result["games"]["1"]
    assert row["last_observation"]["viewer_count"] == 10
    assert row["first_seen_at"] == row["enrollment"]["observed_at"] == "2026-09-26T00:00:00Z"
    assert newer["games"]["1"]["first_seen_at"] == "2026-09-27T16:00:00Z"


def test_stale_publish_does_not_revive_expired_game():
    old = tracking_state()
    newer = tracking_state(at="2026-10-22T17:17:00Z", status="expired")
    assert merge_tracking_state(newer, old)["games"]["1"]["status"] == "expired"


def test_metadata_update_does_not_discard_newer_real_observation():
    newer_metadata = tracking_state(at="2026-09-29T17:17:00Z", status="excluded")
    newer_metadata["games"]["1"]["last_observation"] = {
        "game_id": "1", "viewer_count": 1, "observation_at": "2026-09-27T17:17:00Z"}
    observation = tracking_state(at="2026-09-28T17:17:00Z")
    observation["games"]["1"]["last_observation"] = {
        "game_id": "1", "viewer_count": 100, "observation_at": observation["updated_at"]}
    merged = merge_tracking_state(newer_metadata, observation)["games"]["1"]
    assert merged["status"] == "excluded"
    assert merged["last_observation"]["viewer_count"] == 100


@pytest.mark.parametrize("bad_state", ['{"schema_version": 1, "games": null}', 'null'])
def test_malformed_existing_registry_fails_before_history_or_latest_writes(tmp_path, bad_state):
    path = tmp_path / "data/twitch_tracking.json"
    path.parent.mkdir()
    path.write_text(bad_state)
    before = path.read_bytes()
    with pytest.raises(ValueError):
        store_snapshot(tracked_snapshot(), tmp_path)
    assert list(tmp_path.rglob("*.json")) == [path]
    assert path.read_bytes() == before


def test_registry_loader_pins_frontend_head_without_credentials(monkeypatch):
    sha = "a" * 40
    monkeypatch.setattr(loader.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=f"{sha}\trefs/heads/main\n"))
    urls = []

    def fetch(request, **kwargs):
        urls.append(request.full_url)
        assert not request.has_header("Authorization")
        assert kwargs["timeout"] == 20
        return io.BytesIO(json.dumps(tracking_state()).encode())

    monkeypatch.setattr(loader, "urlopen", fetch)
    assert loader.load_published_tracking() == loader.validate_persisted_tracking(tracking_state())
    assert urls == [f"https://raw.githubusercontent.com/{loader.FRONTEND}/{sha}/data/twitch_tracking.json"]


@pytest.mark.parametrize("status", [404, 403, 429, 500])
def test_unavailable_registry_never_resets_to_empty(monkeypatch, status):
    monkeypatch.setattr(loader.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=f"{'a' * 40}\trefs/heads/main"))

    def fail(*args, **kwargs):
        raise HTTPError("url", status, "unavailable", {}, None)

    monkeypatch.setattr(loader, "urlopen", fail)
    with pytest.raises(HTTPError):
        loader.load_published_tracking()


@pytest.mark.parametrize("state", [None, {}, {"schema_version": 1, "games": {}, "updated_at": None},
                                   {"schema_version": 1, "games": [], "updated_at": "2026-09-28T17:17:00Z"}])
def test_invalid_registry_fails_closed(state):
    with pytest.raises(ValueError):
        loader.validate_persisted_tracking(state)
