from copy import deepcopy
from datetime import timedelta

import pytest

from collectors.steam_twitch_mapping import METHOD, normalize_steam_catalog
from collectors.twitch_candidates import collect_candidates
from collectors.twitch_newness import timestamp
from collectors.twitch_tracking import (
    enroll_observation, enroll_steam_mapping, normalize_tracking_state,
    reconcile_steam_catalog, reconcile_tracking_entry,
)
from tests.test_twitch_candidates import NOW, FakeClient, game, observed, page, registry, stream, stub_hints
from tests.test_twitch_tracking import initial_state


def catalog(*rows):
    return {"version": 2, "generated_at": timestamp(NOW), "count": len(rows), "games": list(rows)}


def steam_row(appid="100", released=None):
    released = released or NOW - timedelta(days=5)
    taipei_day = (released + timedelta(hours=8)).date().isoformat()
    return {"appid": int(appid), "name": "Steam game", "display_name": "繁中遊戲",
            "name_en": "English game", "followers": 5001,
            "release_precision": "day", "release_start": taipei_day, "release_end": taipei_day,
            "release_date_timezone": "Asia/Taipei", "release_time_utc": timestamp(released),
            "tags": ["Action"], "tag_labels_zh_tw": {"Action": "動作"}}


def mapping_for(steam, game_id="1"):
    return {"steam_appid": steam["steam_appid"], "steam": deepcopy(steam), "status": "matched",
            "igdb_id": "101", "twitch_game_id": game_id, "twitch_name": "Twitch game",
            "box_art_url": "https://static-cdn.jtvnw.net/boxart/twitch-{width}x{height}.jpg",
            "checked_at": timestamp(NOW), "retry_at": None, "method": METHOD}


def mapping_state(payload, game_id="1"):
    return {"schema_version": 1, "updated_at": timestamp(NOW),
            "games": {steam["steam_appid"]: mapping_for(steam, game_id)
                      for steam in normalize_steam_catalog(payload, NOW)}}


def stub_mapping(monkeypatch, *, matched=True):
    def refresh(client, payload, previous, now, **kwargs):
        result = mapping_state(payload)
        if not matched:
            for entry in result["games"].values():
                entry.update(status="unmatched", twitch_game_id=None, igdb_id=None)
        return result
    monkeypatch.setattr("collectors.steam_twitch_mapping.refresh_mappings", refresh)


def collect(tmp_path, client, payload=None, tracking=None, **kwargs):
    return collect_candidates(client_id="test", client_secret="test", client=client, now=NOW,
                              registry_path=registry(tmp_path), steam_catalog=payload,
                              tracking_state=tracking, include_release_hints=False, **kwargs)


def test_steam_port_with_old_global_date_and_not_new_bypasses_admission_filters(tmp_path, monkeypatch):
    from scripts.store_twitch_snapshot import validate_snapshot
    stub_mapping(monkeypatch)
    stub_hints(monkeypatch, {"101": timestamp(NOW - timedelta(days=500))})
    client = FakeClient([("games/top", page([game(1, "101")])), ("streams", page([stream(1, 11, 200)]))])
    result = collect_candidates(client_id="test", client_secret="test", client=client, now=NOW,
                                registry_path=registry(tmp_path, {"1": observed("not_new")}),
                                steam_catalog=catalog(steam_row()))
    assert result["candidate_games"] == [] and result["excluded_games"] == []
    row = result["tracked_games"][0]
    assert row["viewer_count"] == 200 and row["verification"]["status"] == "not_new"
    assert set(row["tracking"]["tracking_sources"]) == {"steam:100"}
    assert row["tracking"]["release_at"] == timestamp(NOW - timedelta(days=5))
    assert row["steam_matches"][0]["display_name"] == "繁中遊戲"
    validate_snapshot(result)


@pytest.mark.parametrize("counts", [[], [400]])
def test_two_steam_appids_and_twitch_membership_share_one_census_and_followers(tmp_path, monkeypatch, counts):
    from collectors.twitch_audience import FollowerResolver
    stub_mapping(monkeypatch)
    looked_up = []
    monkeypatch.setattr(FollowerResolver, "resolve", lambda self, user_id: looked_up.append(user_id) or 2001)
    client = FakeClient([("games/top", page([])), ("games", page([game(1, "101")])),
                         ("streams", page([stream(1, 11, count) for count in counts]))])
    result = collect(tmp_path, client, catalog(steam_row("100"), steam_row("200")), initial_state(),
                     include_filtered_audience=True, followers_cache_path=tmp_path / "followers.json")
    assert len(result["tracked_games"]) == 1
    row = result["tracked_games"][0]
    assert row["viewer_count"] == sum(counts) and row["pagination_complete"] is True
    assert row["median_viewer_count"] == (None if not counts else 400)
    assert set(row["tracking"]["tracking_sources"]) == {"twitch_new", "steam:100", "steam:200"}
    assert looked_up == (["11"] if counts else [])
    assert [endpoint for endpoint, _ in client.calls].count("streams") == 1


def test_global_twitch_expiry_cannot_end_active_steam_release():
    state = initial_state(released=NOW - timedelta(days=30), at=NOW - timedelta(days=2))
    steam = normalize_steam_catalog(catalog(steam_row(released=NOW - timedelta(days=3))), NOW)[0]
    entry = enroll_steam_mapping(state, mapping_for(steam), NOW)
    assert entry["status"] == "active"
    assert entry["tracking_sources"]["twitch_new"]["status"] == "expired"
    assert entry["tracking_sources"]["steam:100"]["status"] == "active"
    assert entry["expires_at"] == timestamp(NOW + timedelta(days=27))
    assert entry["release_at"] == timestamp(NOW - timedelta(days=30))  # Global evidence retained.


def test_steam_expiry_cannot_end_unknown_date_twitch_membership():
    state = initial_state()
    entry = state["games"]["1"]
    entry["tracking_sources"]["twitch_new"].update(release_at=None, release_source=None)
    steam = normalize_steam_catalog(catalog(steam_row(released=NOW - timedelta(days=29))), NOW)[0]
    enroll_steam_mapping(state, mapping_for(steam), NOW)
    reconcile_tracking_entry(entry, NOW + timedelta(days=1))
    assert entry["status"] == "active" and entry["expires_at"] is None
    assert entry["tracking_sources"]["steam:100"]["status"] == "expired"


def test_steam_only_game_can_gain_independent_twitch_membership_later():
    state = normalize_tracking_state(None, NOW)
    steam = normalize_steam_catalog(catalog(steam_row()), NOW)[0]
    entry = enroll_steam_mapping(state, mapping_for(steam), NOW)
    row = {"game_id": "1", "game_name": "Twitch game", "viewer_count": 8000,
           "verification": observed("new")}
    enroll_observation(state, row, NOW)
    assert set(entry["tracking_sources"]) == {"twitch_new", "steam:100"}
    assert entry["tracking_sources"]["twitch_new"]["enrollment"]["viewer_count"] == 8000


def test_catalog_removal_or_delayed_steam_release_keeps_independent_twitch_source():
    state = initial_state()
    payload = catalog(steam_row())
    reconcile_steam_catalog(state, normalize_steam_catalog(payload, NOW), mapping_state(payload), NOW)
    entry = state["games"]["1"]
    reconcile_steam_catalog(state, [], {"games": {}}, NOW)
    assert entry["status"] == "active"
    assert entry["tracking_sources"]["steam:100"]["status_reason"] == "steam_not_in_catalog"
    delayed = catalog(steam_row(released=NOW + timedelta(days=2)))
    reconcile_steam_catalog(state, normalize_steam_catalog(delayed, NOW), mapping_state(delayed), NOW)
    assert entry["status"] == "active"
    assert entry["tracking_sources"]["steam:100"]["status_reason"] == "steam_not_released"
    assert entry["steam_matches"][0]["is_recent"] is False


def test_confirmed_mapping_change_moves_only_steam_membership():
    payload = catalog(steam_row())
    steam_catalog = normalize_steam_catalog(payload, NOW)
    state = initial_state()
    reconcile_steam_catalog(state, steam_catalog, mapping_state(payload), NOW)
    reconcile_steam_catalog(state, steam_catalog, mapping_state(payload, game_id="2"), NOW)
    assert state["games"]["1"]["status"] == "active"
    assert state["games"]["1"]["tracking_sources"]["steam:100"]["status_reason"] == "steam_mapping_changed"
    assert state["games"]["2"]["tracking_sources"]["steam:100"]["status"] == "active"


def test_upcoming_steam_mapping_enriches_twitch_but_does_not_enroll_steam(tmp_path, monkeypatch):
    stub_mapping(monkeypatch)
    future = catalog(steam_row(released=NOW + timedelta(hours=1)))
    client = FakeClient([("games/top", page([game(1, "101")])), ("streams", page([]))])
    result = collect(tmp_path, client, future, initial_state())
    row = result["tracked_games"][0]
    assert set(row["tracking"]["tracking_sources"]) == {"twitch_new"}
    assert row["steam_matches"][0]["is_recent"] is False
    empty = collect(tmp_path, FakeClient([("games/top", page([]))]), future)
    assert empty["tracked_games"] == [] and empty["tracking_state"]["games"] == {}


def test_unmatched_steam_game_is_pending_mapping_not_zero_observation(tmp_path, monkeypatch):
    stub_mapping(monkeypatch, matched=False)
    result = collect(tmp_path, FakeClient([("games/top", page([]))]), catalog(steam_row()))
    assert result["tracked_games"] == [] and result["candidate_games"] == []
    assert result["steam_mapping_state"]["games"]["100"]["status"] == "unmatched"
    assert result["coverage"]["categories_measured"] == 0


def test_missing_catalog_retains_steam_membership_and_console_twitch_game(tmp_path):
    state = initial_state(game_id="2")
    steam = normalize_steam_catalog(catalog(steam_row()), NOW)[0]
    enroll_steam_mapping(state, mapping_for(steam), NOW)
    client = FakeClient([("games/top", page([])), ("games", page([game(2), game(1)])),
                         ("streams", page([])), ("streams", page([]))])
    result = collect(tmp_path, client, tracking=state)
    assert {row["game_id"] for row in result["tracked_games"]} == {"1", "2"}
    assert result["steam_catalog_summary"]["status"] == "not_supplied"
    assert result["tracking_state"]["games"]["1"]["tracking_sources"]["steam:100"]["status"] == "active"


def test_legacy_manual_restore_migrates_without_changing_enrollment():
    state = initial_state()
    entry = state["games"]["1"]
    entry.pop("tracking_sources")
    entry["enrollment"].update(kind="manual_restore", source="historical_manual_restore")
    result = normalize_tracking_state(state, NOW)
    assert result["games"]["1"]["enrollment"]["kind"] == "manual_restore"
    assert result["games"]["1"]["tracking_sources"]["twitch_new"]["enrollment"]["kind"] == "manual_restore"


def test_steam_retention_cannot_bypass_old_global_twitch_admission_filter(tmp_path, monkeypatch):
    stub_mapping(monkeypatch)
    stub_hints(monkeypatch, {"101": timestamp(NOW - timedelta(days=500))})
    client = FakeClient([("games/top", page([game(1, "101")])), ("streams", page([stream(1, 11, 8000)]))])
    result = collect_candidates(client_id="test", client_secret="test", client=client, now=NOW,
                                registry_path=registry(tmp_path, {"1": observed("new")}),
                                steam_catalog=catalog(steam_row()))
    assert result["candidate_games"] == []
    assert set(result["tracked_games"][0]["tracking"]["tracking_sources"]) == {"steam:100"}


def test_first_twitch_admission_enriched_by_upcoming_steam_mapping_same_run(tmp_path, monkeypatch):
    stub_mapping(monkeypatch)
    future = catalog(steam_row(released=NOW + timedelta(days=5)))
    client = FakeClient([("games/top", page([game(1, "101")])), ("streams", page([stream(1, 11, 8000)]))])
    result = collect_candidates(client_id="test", client_secret="test", client=client, now=NOW,
                                registry_path=registry(tmp_path, {"1": observed("new")}),
                                steam_catalog=future, include_release_hints=False)
    row = result["tracked_games"][0]
    assert row["steam_matches"][0]["display_name"] == "繁中遊戲"
    assert set(row["tracking"]["tracking_sources"]) == {"twitch_new"}
    assert result["tracking_state"]["games"]["1"]["last_observation"]["steam_matches"] == row["steam_matches"]
