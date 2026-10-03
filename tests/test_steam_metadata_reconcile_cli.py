"""Offline metadata refresh must never pretend that another census occurred."""
from copy import deepcopy
from datetime import timedelta
import json

import pytest

from collectors.twitch_newness import parse_timestamp
from scripts import reconcile_steam_metadata as command
from tests.test_steam_twitch_pipeline import mapping_state, steam_catalog
from tests.test_twitch_steam_discovery_pipeline import discovery_state
from tests.test_twitch_tracking_storage import tracked_snapshot

NOW = parse_timestamp("2026-09-28T18:00:00Z")


def inputs():
    snapshot = tracked_snapshot(1234, 2, 617)
    registry = snapshot["tracking_state"]
    registry["games"]["1"]["igdb_id"] = "200"
    registry["games"]["1"]["last_observation"]["igdb_id"] = "200"
    for row in snapshot["tracked_games"]:
        row.update(igdb_id="200", raw_twitch_name="Game 1",
                   measurement_detail={"users": [{"id": "5", "viewers": 1234}], "complete": True})
    registry["games"]["1"]["last_observation"] = deepcopy(snapshot["tracked_games"][0])
    snapshot["top_games"] = deepcopy(snapshot["tracked_games"])
    snapshot["excluded_games"] = [{"game_id": "9", "game_name": "Unmeasured category",
                                   "reason": "not_new", "viewer_count": None}]
    public = steam_catalog()
    public["games"][0].update(display_name="最新繁中遊戲名稱", name_en="Steam English name")
    return {"snapshot": snapshot, "tracking": deepcopy(registry),
            "mapping": {"schema_version": 1, "updated_at": None, "games": {}},
            "discovery": discovery_state(), "catalog": public}


def front_directory(tmp_path, bundle):
    paths = {"snapshot": "twitch_live.json", "tracking": "twitch_tracking.json",
             "mapping": "twitch_steam_mapping.json", "discovery": "twitch_steam_discovery.json",
             "catalog": "steam_upcoming.json"}
    data = tmp_path / "data"
    data.mkdir()
    for key, name in paths.items():
        (data / name).write_text(json.dumps(bundle[key], ensure_ascii=False), encoding="utf-8")
    (data / "twitch_collection_status.json").write_text('{"status":"existing receipt"}')
    (data / "twitch_history").mkdir()
    (data / "twitch_history/2026-09-28.json").write_text('{"hours":{"original":1234}}')
    (data / "twitch_followers_cache.json").write_text('{"5":{"followers":2001}}')
    return tmp_path


def file_bytes(frontend):
    return {str(path.relative_to(frontend)): path.read_bytes() for path in frontend.rglob("*.json")}


def block_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Metadata-only command must not perform any network request")

    monkeypatch.setattr("collectors.steam_twitch_mapping._pages", forbidden)
    monkeypatch.setattr("collectors.twitch_live.TwitchClient.__init__", forbidden)
    monkeypatch.setattr("requests.sessions.Session.request", forbidden)
    monkeypatch.setattr("urllib.request.urlopen", forbidden)


def test_cli_cached_reverse_identity_enriches_metadata_and_preserves_all_census(tmp_path, monkeypatch, capsys):
    bundle = inputs()
    frontend = front_directory(tmp_path, bundle)
    original = file_bytes(frontend)
    block_network(monkeypatch)
    monkeypatch.delenv("TWITCH_CLIENT_ID", raising=False)
    monkeypatch.delenv("TWITCH_CLIENT_SECRET", raising=False)
    monkeypatch.setattr("sys.argv", ["reconcile", "--frontend-dir", str(frontend), "--now", NOW.isoformat()])

    command.main()

    report = json.loads(capsys.readouterr().out)
    latest = json.loads((frontend / "data/twitch_live.json").read_text())
    registry = json.loads((frontend / "data/twitch_tracking.json").read_text())
    mapping = json.loads((frontend / "data/twitch_steam_mapping.json").read_text())
    assert report["lookup_count"] == 0 and report["enriched_steam_appids"] == ["100"]
    assert report["enriched_twitch_game_ids"] == ["1"]
    assert report["new_registry_game_ids"] == []
    assert command.measurement_projection(latest) == command.measurement_projection(bundle["snapshot"])
    assert latest["generated_at"] == bundle["snapshot"]["generated_at"]
    assert latest["coverage"] == bundle["snapshot"]["coverage"]
    assert latest["collection_schedule"] == bundle["snapshot"]["collection_schedule"]
    assert latest["tracked_games"][0]["game_name"] == "Game 1"
    assert latest["tracked_games"][0]["steam_matches"][0]["display_name"] == "最新繁中遊戲名稱"
    assert registry["games"]["1"]["game_name"] == "Game 1"
    before_observation = bundle["tracking"]["games"]["1"]["last_observation"]
    after_observation = registry["games"]["1"]["last_observation"]
    assert command._row_measurement(after_observation) == command._row_measurement(before_observation)
    assert after_observation["steam_matches"][0]["display_name"] == "最新繁中遊戲名稱"
    assert after_observation["observation_at"] == before_observation["observation_at"]
    assert latest["tracking_state"] == registry and latest["steam_mapping_state"] == mapping
    final = file_bytes(frontend)
    assert {name for name in original if original[name] != final[name]} == {
        "data/twitch_live.json", "data/twitch_tracking.json", "data/twitch_steam_mapping.json"}


def test_new_recent_steam_enrollment_never_fabricates_an_unmeasured_census(tmp_path, monkeypatch):
    bundle = inputs()
    bundle["mapping"] = mapping_state(game_id="2")
    block_network(monkeypatch)

    latest, registry, mapping, report = command.reconcile_metadata(**bundle, now=NOW)

    assert report["new_registry_game_ids"] == ["2"]
    assert registry["games"]["2"]["status"] == "active"
    assert "last_observation" not in registry["games"]["2"]
    assert [row["game_id"] for row in latest["tracked_games"]] == ["1"]
    assert command.measurement_projection(latest) == command.measurement_projection(bundle["snapshot"])
    assert mapping["games"]["100"]["twitch_game_id"] == "2"


def test_older_than_30_day_authoritative_steam_game_only_enriches_existing_twitch_observation(monkeypatch):
    from tests.test_twitch_steam_metadata_authority import (
        APPID, CHECKED, NOW as clock, TWITCH_ID, authoritative_game, catalog, discoveries, tracking,
    )

    registry = tracking()
    row = {"game_id": TWITCH_ID, "game_name": "Original Twitch English name",
           "igdb_id": registry["games"][TWITCH_ID]["igdb_id"],
           "viewer_count": 123, "streamer_count": 1, "median_viewer_count": 123,
           "pagination_complete": True, "verification": {"status": "pending"},
           "observation_status": "current", "observation_at": CHECKED,
           "measurement_started_at": CHECKED, "measurement_finished_at": CHECKED}
    registry["games"][TWITCH_ID]["last_observation"] = deepcopy(row)
    snapshot = {"schema_version": 2, "generated_at": CHECKED, "min_viewers": 7000,
                "coverage": {"collection_complete": True, "categories_measured": 1},
                "candidate_games": [], "tracked_games": [row], "top_games": [],
                "tracking_state": deepcopy(registry)}
    empty_mapping = {"schema_version": 1, "updated_at": None, "games": {}}
    block_network(monkeypatch)

    latest, registry_after, _, report = command.reconcile_metadata(
        snapshot, registry, empty_mapping, discoveries(), catalog(authoritative_game()), clock)

    steam = latest["tracked_games"][0]["steam_matches"][0]
    assert steam["display_name"] == "鬼武者 Way of the Sword"
    assert steam["is_recent"] is False
    assert steam["release_date_conflict"] is True
    assert steam["release_time_basis"] == "steam_taiwan_store_date_authoritative"
    assert set(registry_after["games"][TWITCH_ID]["tracking_sources"]) == {"twitch_new"}
    assert registry_after["games"][TWITCH_ID]["game_name"] == registry["games"][TWITCH_ID]["game_name"]
    assert command.measurement_projection(latest) == command.measurement_projection(snapshot)
    assert report["enriched_steam_appids"] == [APPID] and report["lookup_count"] == 0


def test_metadata_refresh_can_repeat_after_membership_expires_without_deleting_measurement(monkeypatch):
    bundle = inputs()
    bundle["mapping"] = mapping_state()
    block_network(monkeypatch)
    expired_clock = NOW + timedelta(days=40)

    latest, registry, mapping, report = command.reconcile_metadata(**bundle, now=expired_clock)
    assert registry["games"]["1"]["status"] == "expired"
    assert len(latest["tracked_games"]) == 1
    again, registry_again, _, _ = command.reconcile_metadata(
        latest, registry, mapping, bundle["discovery"], bundle["catalog"], expired_clock + timedelta(hours=1))

    assert registry_again["games"]["1"]["status"] == "expired"
    assert command.measurement_projection(again) == command.measurement_projection(bundle["snapshot"])
    assert again["tracked_games"][0]["viewer_count"] == 1234
    assert again["tracked_games"][0]["observation_at"] == "2026-09-28T17:17:00Z"


@pytest.mark.parametrize("document", ["snapshot", "tracking", "mapping", "discovery", "catalog"])
def test_every_invalid_input_prevents_all_output_writes(tmp_path, document):
    bundle = inputs()
    frontend = front_directory(tmp_path, bundle)
    names = {"snapshot": "twitch_live.json", "tracking": "twitch_tracking.json",
             "mapping": "twitch_steam_mapping.json", "discovery": "twitch_steam_discovery.json",
             "catalog": "steam_upcoming.json"}
    (frontend / "data" / names[document]).write_text("null")
    before = file_bytes(frontend)

    with pytest.raises((ValueError, TypeError)):
        command.reconcile_frontend(frontend, NOW)

    assert file_bytes(frontend) == before


def test_dry_run_validates_and_reports_without_any_writes(tmp_path, monkeypatch):
    frontend = front_directory(tmp_path, inputs())
    before = file_bytes(frontend)
    block_network(monkeypatch)

    report = command.reconcile_frontend(frontend, NOW, dry_run=True)

    assert report["dry_run"] is True and report["enriched_steam_appids"] == ["100"]
    assert file_bytes(frontend) == before


def test_input_race_fails_before_writes(tmp_path, monkeypatch):
    frontend = front_directory(tmp_path, inputs())
    original = file_bytes(frontend)
    real_reconcile = command.reconcile_metadata

    def race(*args, **kwargs):
        result = real_reconcile(*args, **kwargs)
        (frontend / "data/steam_upcoming.json").write_text('{"concurrently_published":true}')
        return result

    monkeypatch.setattr(command, "reconcile_metadata", race)
    with pytest.raises(ValueError, match="changed during"):
        command.reconcile_frontend(frontend, NOW)
    after = file_bytes(frontend)
    assert {name for name in original if original[name] != after[name]} == {"data/steam_upcoming.json"}


def test_clock_cannot_relabel_future_inputs_as_current(tmp_path):
    frontend = front_directory(tmp_path, inputs())
    before = file_bytes(frontend)

    with pytest.raises(ValueError, match="cannot precede"):
        command.reconcile_frontend(frontend, NOW - timedelta(hours=2))

    assert file_bytes(frontend) == before
