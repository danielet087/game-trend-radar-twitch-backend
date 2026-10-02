"""Offline production input and publication race checks for dual sources."""
from copy import deepcopy
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import re
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from scripts import load_twitch_tracking as loader
from scripts.store_twitch_snapshot import merge_mapping_state, merge_tracking_state, store_snapshot
from tests.test_twitch_tracking_storage import tracking_state, tracked_snapshot


def steam_catalog():
    return {"version": 2, "generated_at": "2026-09-28T17:17:00Z", "count": 1, "games": [{
        "appid": 100, "name": "Steam game", "followers": 5000, "release_precision": "day",
        "release_start": "2026-09-20", "release_end": "2026-09-20", "tags": ["Action"],
    }]}


def mapping_state(appid="100", at="2026-09-28T17:17:00Z", *, game_id="1"):
    from collectors.steam_twitch_mapping import METHOD, normalize_steam_catalog

    steam = normalize_steam_catalog(steam_catalog(), datetime(2026, 9, 28, tzinfo=timezone.utc))[0]
    steam["steam_appid"] = appid
    return {"schema_version": 1, "updated_at": at, "games": {appid: {
        "steam_appid": appid, "steam": steam, "status": "matched", "igdb_id": "200",
        "twitch_game_id": game_id, "checked_at": at, "retry_at": None, "method": METHOD,
    }}}


def set_frontend_head(monkeypatch):
    sha = "b" * 40
    monkeypatch.setattr(loader.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=f"{sha}\trefs/heads/main\n"))
    return sha


def test_bundle_uses_one_immutable_commit_and_allows_only_missing_mapping_bootstrap(monkeypatch):
    sha = set_frontend_head(monkeypatch)
    urls = []
    sources = {loader.TRACKING_PATH: tracking_state(), loader.STEAM_PATH: steam_catalog()}

    def fetch(request, **kwargs):
        urls.append(request.full_url)
        assert not request.has_header("Authorization")
        assert f"/{sha}/" in request.full_url
        path = request.full_url.split(f"/{sha}/", 1)[1]
        if path in {loader.MAPPING_PATH, loader.DISCOVERY_PATH}:
            raise HTTPError(request.full_url, 404, "not found", {}, None)
        return io.BytesIO(json.dumps(sources[path]).encode())

    monkeypatch.setattr(loader, "urlopen", fetch)
    bundle = loader.load_published_inputs()
    assert bundle["source_commit"] == sha
    assert bundle["steam_catalog"] == {**steam_catalog(), "_source_commit": sha}
    assert bundle["steam_mapping_state"] == {"schema_version": 1, "updated_at": None, "games": {}}
    assert bundle["steam_discovery_state"] == {"schema_version": 1, "updated_at": None, "games": {}}
    assert len(urls) == 4


@pytest.mark.parametrize("bad", [None, {}, {"schema_version": 1, "updated_at": None, "games": []}])
def test_existing_corrupt_mapping_stops_bundle_without_creating_outputs(tmp_path, monkeypatch, bad):
    sha = set_frontend_head(monkeypatch)
    sources = {loader.TRACKING_PATH: tracking_state(), loader.STEAM_PATH: steam_catalog(), loader.MAPPING_PATH: bad}
    monkeypatch.setattr(loader, "urlopen", lambda request, **k: io.BytesIO(json.dumps(sources[request.full_url.split(f"/{sha}/", 1)[1]]).encode()))
    monkeypatch.setattr("sys.argv", ["loader", "--output", str(tmp_path / "tracking.json"),
                                   "--steam-output", str(tmp_path / "steam.json"), "--mapping-output", str(tmp_path / "mapping.json")])
    with pytest.raises(ValueError):
        loader.main()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("path,status", [(loader.STEAM_PATH, 404), (loader.MAPPING_PATH, 403),
                                          (loader.MAPPING_PATH, 429), (loader.MAPPING_PATH, 500)])
def test_required_catalog_and_non404_mapping_fail_closed(monkeypatch, path, status):
    sha = set_frontend_head(monkeypatch)
    def fetch(request, **kwargs):
        relative = request.full_url.split(f"/{sha}/", 1)[1]
        if relative == path:
            raise HTTPError(request.full_url, status, "failure", {}, None)
        return io.BytesIO(json.dumps(tracking_state() if relative == loader.TRACKING_PATH else steam_catalog()).encode())
    monkeypatch.setattr(loader, "urlopen", fetch)
    with pytest.raises(HTTPError):
        loader.load_published_inputs()


def test_mapping_race_unions_new_ids_without_rewinding_corrected_steam_date():
    old = mapping_state(at="2026-09-28T17:17:00Z")
    newer = deepcopy(old)
    newer["updated_at"] = "2026-09-29T17:17:00Z"
    newer["games"]["100"]["steam"].update(release_at="2026-11-01T00:00:00Z", is_recent=False)
    old["games"].update(mapping_state("101")["games"])
    result = merge_mapping_state(newer, old)
    assert set(result["games"]) == {"100", "101"}
    assert result["games"]["100"]["steam"]["release_at"] == "2026-11-01T00:00:00Z"
    assert result["games"]["100"]["steam"]["is_recent"] is False
    assert result["updated_at"] == newer["updated_at"]


def test_same_twitch_id_race_retains_twitch_and_steam_sources_and_expired_tombstone():
    newer = loader.validate_persisted_tracking(tracking_state(at="2026-09-29T17:17:00Z"))
    stale = loader.validate_persisted_tracking(tracking_state())
    steam = mapping_state()["games"]["100"]["steam"]
    stale["games"]["1"]["tracking_sources"]["steam:100"] = {
        "source": "steam_recent_release", "steam_appid": "100", "steam": steam,
        "first_seen_at": stale["updated_at"], "updated_at": stale["updated_at"],
        "release_at": steam["release_at"], "release_source": "steam_taiwan_store",
        "expires_at": steam["expires_at"], "status": "active", "status_reason": "within_release_window",
        "enrollment": {"source": "steam_recent_release", "observed_at": stale["updated_at"]},
    }
    merged = merge_tracking_state(newer, stale)
    assert set(merged["games"]["1"]["tracking_sources"]) == {"twitch_new", "steam:100"}
    assert merged["games"]["1"]["status"] == "active"
    terminal = deepcopy(merged)
    terminal["updated_at"] = "2026-10-22T17:17:00Z"
    terminal["games"]["1"]["updated_at"] = terminal["updated_at"]
    for source in terminal["games"]["1"]["tracking_sources"].values():
        source.update(status="expired", updated_at=terminal["updated_at"])
    result = merge_tracking_state(terminal, stale)
    assert result["games"]["1"]["status"] == "expired"
    assert all(source["status"] == "expired" for source in result["games"]["1"]["tracking_sources"].values())


def test_corrupt_published_mapping_fails_before_any_snapshot_writes(tmp_path):
    existing = tmp_path / "data/twitch_steam_mapping.json"
    existing.parent.mkdir()
    existing.write_text("null")
    payload = tracked_snapshot()
    payload["steam_mapping_state"] = mapping_state()
    with pytest.raises(ValueError):
        store_snapshot(payload, tmp_path)
    assert list(tmp_path.rglob("*.json")) == [existing]
    assert existing.read_text() == "null"


def test_mapping_and_sources_are_archived_with_real_observation_and_receipt(tmp_path):
    payload = tracked_snapshot(0, 0, None)
    payload["steam_mapping_state"] = mapping_state()
    payload["tracked_games"][0]["steam_matches"] = [mapping_state()["games"]["100"]["steam"]]
    relative = store_snapshot(payload, tmp_path)
    assert (tmp_path / "data/twitch_steam_mapping.json").exists()
    assert (tmp_path / "data/twitch_tracking.json").exists()
    assert (tmp_path / "data/twitch_collection_status.json").exists()
    saved = json.loads((tmp_path / relative).read_text())["hours"]["2026-09-28T16:00:00Z"]["games"]
    assert len(saved) == 1 and saved[0]["viewer_count"] == 0
    assert saved[0]["steam_matches"] == payload["tracked_games"][0]["steam_matches"]


def test_production_workflow_loads_dual_inputs_and_module_push_bootstraps_only():
    text = (Path(__file__).resolve().parents[1] / ".github/workflows/collect.yml").read_text()
    assert re.findall(r"cron:\s*'([^']+)'", text) == ["17 * * * *"]
    assert re.search(r"\n  push:\n    branches: \[main\]\n    paths:\n      - 'collectors/steam_twitch_mapping.py'\n      - 'collectors/twitch_steam_discovery.py'\n  schedule:", text)
    load = next(line for line in text.splitlines() if "run: python -m scripts.load_twitch_tracking" in line)
    collect = next(line for line in text.splitlines() if "run: python -m scripts.update_twitch" in line)
    assert "--steam-output output/steam_catalog_input.json" in load
    assert "--mapping-output output/twitch_steam_mapping_input.json" in load
    assert "--discovery-output output/twitch_steam_discovery_input.json" in load
    assert "--steam-catalog output/steam_catalog_input.json" in collect
    assert "--steam-mapping output/twitch_steam_mapping_input.json" in collect
    assert "--steam-discovery output/twitch_steam_discovery_input.json" in collect
    assert "FORCE_COLLECTION: ${{ inputs.force || github.event_name == 'push' || false }}" in text
