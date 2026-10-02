"""Reverse-discovery metadata must survive production loading and publish races."""
from copy import deepcopy
from datetime import datetime, timezone
import io
import json
import sys
from urllib.error import HTTPError

import pytest

from collectors.twitch_candidates import collect_candidates
from scripts import load_twitch_tracking as loader
from scripts.store_twitch_snapshot import merge_discovery_state, store_snapshot
from tests.test_steam_twitch_pipeline import mapping_state, set_frontend_head, steam_catalog
from tests.test_twitch_candidates import NOW, FakeClient, game, observed, page, registry, stream
from tests.test_twitch_tracking_storage import tracked_snapshot, tracking_state


def discovery_state(game_id="1", at="2026-09-28T17:17:00Z", *, appid="100", status="matched"):
    matched = status == "matched"
    enrollment = tracking_state()["games"]["1"]["enrollment"]
    return {"schema_version": 1, "updated_at": at, "steam_source_id": "8",
            "source_catalog": {"generated_at": at, "count": 0, "appids": []}, "games": {game_id: {
        "twitch_game_id": game_id, "twitch_name": f"Game {game_id}", "igdb_id": "200",
        "steam_appids": [appid] if matched else [],
        "links": [{"external_game_id": "300", "external_game_source": "8", "uid": appid,
                   "game": "200", "steam_appid": appid,
                   "url": f"https://store.steampowered.com/app/{appid}/"}] if matched else [],
        "status": status, "method": "twitch_igdb_external_steam_v1", "active": True,
        "first_seen_at": "2026-09-27T16:00:00Z", "checked_at": at if status != "unavailable" else None,
        "updated_at": at, "retry_at": None, "twitch_enrollment": enrollment,
        "public_steam_appids": [], "missing_public_appids": [appid] if matched else [],
    }}}


def test_four_inputs_use_one_commit_including_nonempty_discovery(monkeypatch):
    sha = set_frontend_head(monkeypatch)
    sources = {loader.TRACKING_PATH: tracking_state(), loader.STEAM_PATH: steam_catalog(),
               loader.MAPPING_PATH: mapping_state(), loader.DISCOVERY_PATH: discovery_state()}
    urls = []

    def fetch(request, **kwargs):
        urls.append(request.full_url)
        assert f"/{sha}/" in request.full_url
        return io.BytesIO(json.dumps(sources[request.full_url.split(f"/{sha}/", 1)[1]]).encode())

    monkeypatch.setattr(loader, "urlopen", fetch)
    result = loader.load_published_inputs()
    assert result["steam_discovery_state"] == loader.validate_persisted_discovery(discovery_state())
    assert len(urls) == 4


@pytest.mark.parametrize("bad", [None, {}, {"schema_version": 1, "updated_at": None, "games": []}])
def test_corrupt_discovery_prevents_all_bundle_output_writes(tmp_path, monkeypatch, bad):
    sha = set_frontend_head(monkeypatch)
    sources = {loader.TRACKING_PATH: tracking_state(), loader.STEAM_PATH: steam_catalog(),
               loader.MAPPING_PATH: mapping_state(), loader.DISCOVERY_PATH: bad}
    monkeypatch.setattr(loader, "urlopen", lambda request, **kwargs:
                        io.BytesIO(json.dumps(sources[request.full_url.split(f"/{sha}/", 1)[1]]).encode()))
    monkeypatch.setattr(sys, "argv", ["loader", "--output", str(tmp_path / "tracking.json"),
                                     "--steam-output", str(tmp_path / "catalog.json"),
                                     "--mapping-output", str(tmp_path / "mapping.json"),
                                     "--discovery-output", str(tmp_path / "discovery.json")])
    with pytest.raises(ValueError):
        loader.main()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("status", [403, 429, 500])
def test_discovery_errors_other_than_first_bootstrap_404_fail_closed(monkeypatch, status):
    sha = set_frontend_head(monkeypatch)
    sources = {loader.TRACKING_PATH: tracking_state(), loader.STEAM_PATH: steam_catalog(),
               loader.MAPPING_PATH: mapping_state()}

    def fetch(request, **kwargs):
        relative = request.full_url.split(f"/{sha}/", 1)[1]
        if relative == loader.DISCOVERY_PATH:
            raise HTTPError(request.full_url, status, "not usable", {}, None)
        return io.BytesIO(json.dumps(sources[relative]).encode())

    monkeypatch.setattr(loader, "urlopen", fetch)
    with pytest.raises(HTTPError):
        loader.load_published_inputs()


def test_new_twitch_enrollment_enters_reverse_lookup_in_the_same_run(tmp_path, monkeypatch):
    captured = {}
    catalog = {"version": 2, "generated_at": "2026-09-28T17:17:00Z", "count": 0, "games": []}
    previous = {"schema_version": 1, "updated_at": None, "games": {}}
    monkeypatch.setattr("collectors.steam_twitch_mapping.refresh_mappings",
                        lambda *args, **kwargs: {"schema_version": 1, "updated_at": None, "games": {}})

    def refresh(client, tracking, public_catalog, state, now, *, deadline, monotonic):
        captured.update(tracking=deepcopy(tracking), catalog=public_catalog, previous=state,
                        deadline=deadline, clock=monotonic())
        result = discovery_state(at=now.isoformat().replace("+00:00", "Z"))
        result["games"]["1"]["twitch_enrollment"] = deepcopy(
            tracking["games"]["1"]["tracking_sources"]["twitch_new"]["enrollment"])
        return result

    monkeypatch.setattr("collectors.twitch_steam_discovery.refresh_discoveries", refresh)
    client = FakeClient([("games/top", page([game(1, "200")])),
                         ("streams", page([stream(1, 11, 8000)]))])
    result = collect_candidates(client_id="test", client_secret="test", client=client, now=NOW,
                                registry_path=registry(tmp_path, {"1": observed("new")}),
                                include_release_hints=False, steam_catalog=catalog,
                                steam_discovery_state=previous)
    source = captured["tracking"]["games"]["1"]["tracking_sources"]["twitch_new"]
    assert source["status"] == "active" and source["enrollment"]["viewer_count"] == 8000
    assert captured["catalog"] == catalog and captured["previous"] == previous
    assert captured["deadline"] > captured["clock"]
    assert len(result["tracked_games"]) == 1
    assert result["tracked_games"][0]["viewer_count"] == 8000
    assert result["steam_discovery_state"]["games"]["1"]["steam_appids"] == ["100"]
    assert catalog["games"] == []


def test_unavailable_reverse_metadata_keeps_real_census_and_previous_link(tmp_path, monkeypatch):
    catalog = {"version": 2, "generated_at": "2026-09-28T17:17:00Z", "count": 0, "games": []}
    previous = discovery_state()
    monkeypatch.setattr("collectors.steam_twitch_mapping.refresh_mappings",
                        lambda *args, **kwargs: {"schema_version": 1, "updated_at": None, "games": {}})

    def partial(*args, **kwargs):
        result = deepcopy(previous)
        result["report"] = {"status": "unavailable_or_partial", "errors": [{"stage": "external_games", "reason": "HTTP 503"}]}
        return result

    monkeypatch.setattr("collectors.twitch_steam_discovery.refresh_discoveries", partial)
    client = FakeClient([("games/top", page([game(1, "200")])),
                         ("streams", page([stream(1, 11, 8000)]))])
    result = collect_candidates(client_id="test", client_secret="test", client=client, now=NOW,
                                registry_path=registry(tmp_path, {"1": observed("new")}),
                                include_release_hints=False, steam_catalog=catalog,
                                steam_discovery_state=previous)
    assert result["coverage"]["collection_complete"] is True
    assert result["tracked_games"][0]["viewer_count"] == 8000
    assert result["steam_discovery_state"]["games"]["1"]["status"] == "matched"
    assert result["steam_discovery_summary"]["status"] == "unavailable_or_partial"


def test_discovery_race_unions_new_ids_preserves_newer_link_and_earliest_evidence():
    newer = discovery_state(at="2026-09-29T17:17:00Z")
    stale = discovery_state(at="2026-09-28T17:17:00Z", status="no_steam_link")
    stale["games"]["1"]["first_seen_at"] = "2026-09-26T16:00:00Z"
    stale["games"].update(discovery_state("2")["games"])
    result = merge_discovery_state(newer, stale)
    assert set(result["games"]) == {"1", "2"}
    assert result["games"]["1"]["status"] == "matched"
    assert result["games"]["1"]["steam_appids"] == ["100"]
    assert result["games"]["1"]["first_seen_at"] == "2026-09-26T16:00:00Z"
    assert result["updated_at"] == newer["updated_at"]


def test_newer_unavailable_does_not_displace_successful_identity_but_updates_membership():
    matched = discovery_state()
    failed = discovery_state(at="2026-09-29T17:17:00Z", status="unavailable")
    result = merge_discovery_state(matched, failed)["games"]["1"]
    assert result["status"] == "matched" and result["steam_appids"] == ["100"]
    assert result["checked_at"] == matched["games"]["1"]["checked_at"]
    assert result["updated_at"] == failed["games"]["1"]["updated_at"]


def test_newer_complete_negative_lookup_can_replace_a_previous_link():
    matched = discovery_state()
    absent = discovery_state(at="2026-09-29T17:17:00Z", status="no_steam_link")
    result = merge_discovery_state(matched, absent)["games"]["1"]
    assert result["status"] == "no_steam_link" and result["steam_appids"] == []


def test_cached_identity_cannot_rewind_fresher_public_membership():
    stale = discovery_state()
    newer = deepcopy(stale)
    newer["updated_at"] = newer["games"]["1"]["updated_at"] = "2026-09-29T17:17:00Z"
    newer["source_catalog"] = {"generated_at": newer["updated_at"], "count": 1, "appids": ["100"]}
    newer["games"]["1"].update(public_steam_appids=["100"], missing_public_appids=[], active=False)
    result = merge_discovery_state(newer, stale)["games"]["1"]
    assert result["public_steam_appids"] == ["100"] and result["missing_public_appids"] == []
    assert result["active"] is False


def test_changed_identity_uses_latest_complete_catalog_instead_of_old_projection():
    identity = discovery_state(at="2026-09-29T17:17:00Z", appid="100")
    stale_identity = discovery_state(at="2026-09-28T17:17:00Z", appid="200")
    stale_identity["updated_at"] = stale_identity["games"]["1"]["updated_at"] = "2026-09-30T17:17:00Z"
    stale_identity["source_catalog"] = {"generated_at": "2026-09-30T17:17:00Z", "count": 1, "appids": ["100"]}
    merged = merge_discovery_state(identity, stale_identity)
    row = merged["games"]["1"]
    assert row["steam_appids"] == row["public_steam_appids"] == ["100"]
    assert row["missing_public_appids"] == []
    assert merged["source_catalog"] == stale_identity["source_catalog"]


def test_later_job_with_older_catalog_cannot_rewind_public_membership():
    recent_catalog = discovery_state(at="2026-09-29T17:17:00Z")
    recent_catalog["source_catalog"] = {"generated_at": "2026-09-29T17:17:00Z", "count": 1, "appids": ["100"]}
    recent_catalog["games"]["1"].update(public_steam_appids=["100"], missing_public_appids=[])
    older_catalog = discovery_state(at="2026-09-30T17:17:00Z")
    older_catalog["source_catalog"]["generated_at"] = "2026-09-28T17:17:00Z"
    merged = merge_discovery_state(recent_catalog, older_catalog)
    assert merged["games"]["1"]["public_steam_appids"] == ["100"]
    assert merged["games"]["1"]["missing_public_appids"] == []
    assert merged["source_catalog"] == recent_catalog["source_catalog"]


def test_corrupt_existing_discovery_prevents_all_snapshot_writes(tmp_path):
    existing = tmp_path / "data/twitch_steam_discovery.json"
    existing.parent.mkdir()
    existing.write_text("null")
    payload = tracked_snapshot()
    payload["steam_discovery_state"] = discovery_state()
    with pytest.raises(ValueError):
        store_snapshot(payload, tmp_path)
    assert list(tmp_path.rglob("*.json")) == [existing]
    assert existing.read_text() == "null"


def test_discovery_and_census_are_published_together_without_touching_steam_catalog(tmp_path):
    catalog_path = tmp_path / "data/steam_upcoming.json"
    catalog_path.parent.mkdir()
    catalog_path.write_text(json.dumps(steam_catalog()))
    before = catalog_path.read_bytes()
    payload = tracked_snapshot(0, 0, None)
    payload["steam_discovery_state"] = discovery_state()
    relative = store_snapshot(payload, tmp_path)
    saved_discovery = json.loads((tmp_path / "data/twitch_steam_discovery.json").read_text())
    latest = json.loads((tmp_path / "data/twitch_live.json").read_text())
    assert latest["steam_discovery_state"] == saved_discovery
    history = json.loads((tmp_path / relative).read_text())
    assert history["hours"]["2026-09-28T16:00:00Z"]["games"][0]["viewer_count"] == 0
    assert (tmp_path / "data/twitch_tracking.json").exists()
    assert (tmp_path / "data/twitch_collection_status.json").exists()
    assert catalog_path.read_bytes() == before


def test_production_cli_requires_discovery_input_before_api_calls(tmp_path, monkeypatch):
    from scripts import update_twitch

    paths = {}
    for name, value in (("tracking", tracking_state()), ("catalog", steam_catalog()), ("mapping", mapping_state())):
        paths[name] = tmp_path / f"{name}.json"
        paths[name].write_text(json.dumps(value))
    monkeypatch.setenv("TWITCH_CLIENT_ID", "fixture-client")
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", "fixture-secret")
    monkeypatch.setenv("GITHUB_RUN_ID", "fixture-run")
    monkeypatch.setattr(sys, "argv", ["collector", "--tracking-state", str(paths["tracking"]),
                                     "--steam-catalog", str(paths["catalog"]), "--steam-mapping", str(paths["mapping"])])
    monkeypatch.setattr(update_twitch, "collect_twitch", lambda **kwargs: pytest.fail("Must not call API"))
    with pytest.raises(SystemExit, match="steam-discovery"):
        update_twitch.main()
