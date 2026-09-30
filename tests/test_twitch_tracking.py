from copy import deepcopy
from datetime import timedelta

import pytest

from collectors.twitch_candidates import NON_GAME_IDS, IncompleteCollection, collect_candidates
from collectors.twitch_newness import timestamp
from collectors.twitch_tracking import enroll_observation, normalize_tracking_state
from tests.test_twitch_candidates import NOW, FakeClient, game, observed, page, registry, stream, stub_hints


def initial_state(*, game_id="1", released=None, viewers=8000, at=NOW - timedelta(days=1)):
    released = released or NOW - timedelta(days=5)
    state = normalize_tracking_state(None, at)
    row = {
        "game_id": game_id, "game_name": "Tracked game", "igdb_id": "101",
        "viewer_count": viewers, "streamer_count": 1, "median_viewer_count": viewers,
        "verification": {"status": "pending"},
        "release_evidence": {"first_release_date": timestamp(released), "checked_at": timestamp(at)},
    }
    assert enroll_observation(state, row, at)
    return state


def run(tmp_path, client, state, **kwargs):
    return collect_candidates(client_id="test", client_secret="test", client=client, now=NOW,
                              registry_path=registry(tmp_path), tracking_state=state,
                              include_release_hints=False, **kwargs)


def test_tracked_below_threshold_kept_and_measured_once(tmp_path):
    state = initial_state()
    original = deepcopy(state)
    client = FakeClient([("games/top", page([game(1, "101")])), ("streams", page([stream(1, 11, 400)]))])
    result = run(tmp_path, client, state)
    assert result["candidate_games"] == []
    assert result["tracked_games"][0]["viewer_count"] == 400
    assert result["tracked_games"][0]["tracking"]["first_seen_at"] == original["games"]["1"]["first_seen_at"]
    assert [endpoint for endpoint, _ in client.calls] == ["games/top", "streams"]
    assert state == original  # No state mutation can escape an unsuccessful collection.


@pytest.mark.parametrize("streams,viewers,streamers", [([], 0, 0), ([stream(1, 1, 50)], 50, 1)])
def test_off_directory_tracked_game_is_still_collected(tmp_path, streams, viewers, streamers):
    client = FakeClient([
        ("games/top", page([])), ("games", page([game(1, "101")])), ("streams", page(streams)),
    ])
    result = run(tmp_path, client, initial_state())
    row = result["tracked_games"][0]
    assert row["viewer_count"] == viewers and row["streamer_count"] == streamers
    assert row["median_viewer_count"] == (None if not streams else 50)
    assert row["observation_status"] == "current" and row["pagination_complete"] is True
    assert result["coverage"]["tracked_outside_discovery_count"] == 1
    assert client.calls[1][1] == {"id": ["1"]}


@pytest.mark.parametrize("viewers", [500, 8000])
def test_active_tracking_survives_badge_not_new_and_no_release_lookup(tmp_path, viewers):
    from scripts.store_twitch_snapshot import validate_snapshot
    client = FakeClient([("games/top", page([game(1, "101")])), ("streams", page([stream(1, 1, viewers)]))])
    result = collect_candidates(client_id="test", client_secret="test", client=client, now=NOW,
                                registry_path=registry(tmp_path, {"1": observed("not_new")}),
                                tracking_state=initial_state(), include_release_hints=False)
    assert result["excluded_games"] == []
    assert result["tracked_games"][0]["verification"]["status"] == "not_new"
    assert result["tracking_state"]["games"]["1"]["status"] == "active"
    assert result["candidate_games"] == []
    validate_snapshot(result)


def test_known_thirty_day_boundary_expires_without_any_live_request(tmp_path):
    state = initial_state(released=NOW - timedelta(days=30), at=NOW - timedelta(days=2))
    client = FakeClient([("games/top", page([]))])
    result = run(tmp_path, client, state)
    assert result["tracked_games"] == []
    assert result["tracking_state"]["games"]["1"]["status"] == "expired"
    assert len(client.calls) == 1


def test_unknown_release_stays_active_until_date_can_be_resolved(tmp_path):
    state = initial_state()
    entry = state["games"]["1"]
    entry.update(release_at=None, expires_at=None, release_source=None)
    entry["last_observation"].pop("release_evidence")
    client = FakeClient([("games/top", page([])), ("games", page([])), ("streams", page([]))])
    result = run(tmp_path, client, state)
    assert result["tracking_state"]["games"]["1"]["status"] == "active"
    assert result["tracked_games"][0]["tracking"]["status_reason"] == "awaiting_release_date"


def test_metadata_failure_keeps_cached_release_and_unknown_never_becomes_zero(tmp_path, monkeypatch):
    client = FakeClient([("games/top", page([])), ("games", page([game(1, "101")])), ("streams", {"data": None})])
    stub_hints(monkeypatch, {}, status="unavailable_or_partial")
    state = initial_state()
    original = deepcopy(state)
    with pytest.raises(IncompleteCollection, match="Malformed"):
        collect_candidates(client_id="test", client_secret="test", client=client, now=NOW,
                           registry_path=registry(tmp_path), tracking_state=state)
    assert state == original


def test_release_correction_can_extend_existing_tracking(tmp_path, monkeypatch):
    state = initial_state(released=NOW - timedelta(days=29))
    client = FakeClient([("games/top", page([])), ("games", page([game(1, "101")])), ("streams", page([]))])
    new_date = timestamp(NOW + timedelta(days=5))
    stub_hints(monkeypatch, {"101": new_date})
    result = collect_candidates(client_id="test", client_secret="test", client=client, now=NOW,
                                registry_path=registry(tmp_path), tracking_state=state)
    assert result["tracking_state"]["games"]["1"]["release_at"] == new_date
    assert result["tracking_state"]["games"]["1"]["expires_at"] == timestamp(NOW + timedelta(days=35))


def test_popular_unknown_category_not_admitted_but_signal_game_is(tmp_path, monkeypatch):
    client = FakeClient([("games/top", page([game(1, "101"), game(2, "102")])),
                         ("streams", page([stream(1, 1, 8000)])), ("streams", page([stream(2, 2, 8000)]))])
    stub_hints(monkeypatch, {"102": timestamp(NOW - timedelta(days=20))})
    result = collect_candidates(client_id="test", client_secret="test", client=client, now=NOW,
                                registry_path=registry(tmp_path))
    assert len(result["candidate_games"]) == 2
    assert [row["game_id"] for row in result["tracked_games"]] == ["2"]


def test_history_backfill_reevaluates_saved_igdb_fourteen_day_miss():
    state = normalize_tracking_state(None, NOW)
    row = {"game_id": "1", "game_name": "Previously missed", "viewer_count": 8000,
           "verification": {"status": "pending"}, "release_experiment": {
               "igdb_first_release_date": {"status": "evaluated", "predicted_new": False,
                   "window_days": 14, "release_at": timestamp(NOW - timedelta(days=20)),
                   "metadata_observed_at": timestamp(NOW)}}}
    result = enroll_observation(state, row, NOW)
    assert result["status"] == "active"
    assert result["enrollment"]["source"] == "igdb_first_release_date"
    assert row["release_experiment"]["igdb_first_release_date"]["predicted_new"] is False


def test_persisted_non_game_is_removed_from_active_set_without_collection(tmp_path):
    state = initial_state(game_id="26936")
    # This exercises corrupt/legacy state as well as new admission rejection.
    result = run(tmp_path, FakeClient([("games/top", page([]))]), state)
    assert result["tracked_games"] == []
    assert result["tracking_state"]["games"]["26936"]["status"] == "excluded"


def test_truncated_or_mismatched_state_fails_closed():
    state = initial_state()
    state["games"]["1"]["last_observation"]["game_id"] = "2"
    with pytest.raises(ValueError, match="identity"):
        normalize_tracking_state(state)


def test_candidate_and_tracked_union_resolves_followers_once(tmp_path, monkeypatch):
    from collectors.twitch_audience import FollowerResolver
    looked_up = []
    monkeypatch.setattr(FollowerResolver, "resolve", lambda self, user_id: looked_up.append(user_id) or 2001)
    client = FakeClient([("games/top", page([game(1, "101")])), ("streams", page([stream(1, 11, 8000)]))])
    result = run(tmp_path, client, initial_state(), include_filtered_audience=True,
                 followers_cache_path=tmp_path / "followers.json")
    assert looked_up == ["11"]
    assert result["candidate_games"][0] is result["tracked_games"][0]
    assert result["tracked_games"][0]["filtered_audience"]["median_viewer_count"] == 8000
