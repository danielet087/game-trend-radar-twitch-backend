"""Exercise the migrated identity graph through collection and local storage."""

from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import subprocess
import sys

import pytest
import requests

from tests.test_twitch_collection_composition import (
    FakeClient,
    FrozenDateTime,
    NOW,
    STAMP,
    badge,
    dump,
)


def test_identity_and_publication_graph_runs_without_legacy_owners():
    code = """
import importlib
import importlib.abc
import sys
blocked = {'collectors.twitch_candidates', 'collectors.twitch_audience',
           'collectors.twitch_tracking', 'collectors.twitch_newness',
           'collectors.twitch_live', 'scripts.load_twitch_tracking',
           'collectors.steam_twitch_mapping', 'collectors.twitch_steam_discovery',
           'collectors.twitch_steam_website_identity', 'scripts.store_twitch_snapshot',
           'collectors.twitch_steam_admission'}
class BlockLegacy(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in blocked:
            raise AssertionError('legacy dependency: ' + fullname)
sys.meta_path.insert(0, BlockLegacy())
for name in ('radar_backend.jobs.collect_twitch', 'radar_backend.jobs.load_frontend_inputs',
             'radar_backend.jobs.store_twitch_snapshot', 'radar_backend.jobs.publish_twitch',
             'radar_backend.adapters.twitch_candidates', 'radar_backend.adapters.frontend_inputs',
             'radar_backend.adapters.steam_twitch_mapping',
             'radar_backend.adapters.twitch_steam_discovery',
             'radar_backend.adapters.twitch_steam_website_identity',
             'radar_backend.adapters.twitch_snapshot', 'radar_backend.publication.twitch',
             'scripts.update_twitch', 'scripts.reconcile_steam_metadata',
             'scripts.recover_twitch_tracking', 'scripts.import_twitch_release_dates',
             'scripts.probe_igdb_hypes'):
    importlib.import_module(name)
from radar_backend.adapters import twitch_candidates, steam_twitch_mapping, twitch_steam_discovery
from radar_backend.adapters import twitch_snapshot
from radar_backend.jobs import store_twitch_snapshot
from radar_backend.publication import twitch
assert twitch_candidates._mapping_callbacks()[2] is steam_twitch_mapping.refresh_mappings
assert twitch_candidates._discovery_callback() is twitch_steam_discovery.refresh_discoveries
assert store_twitch_snapshot.store_snapshot is twitch.store_snapshot is twitch_snapshot.store_snapshot
assert steam_twitch_mapping.normalize_mapping_state(None)['games'] == {}
assert twitch_steam_discovery.normalize_discovery_state(None)['games'] == {}
assert not blocked.intersection(sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


class IdentityClient(FakeClient):
    timeout_seconds = 20

    def __init__(self, *, unavailable):
        super().__init__()
        self.request_interval = 0.3
        self.unavailable = unavailable

    def _wait(self):
        pass

    def post(self, url, **options):
        endpoint = url.rsplit("/", 1)[1]
        self.calls.append((endpoint, options["data"], {}))
        if self.unavailable:
            raise requests.ConnectionError("official identity service unavailable")
        if endpoint == "external_game_sources":
            payload = [{"id": 45, "name": "Steam"}]
        else:
            assert endpoint == "external_games"
            payload = [{"id": 88, "uid": "100", "game": 77, "external_game_source": 45}]

        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return payload

        return Response()

    def get(self, endpoint, *, params, **options):
        if endpoint == "games":
            self.calls.append((endpoint, deepcopy(params), options))
            return {
                "data": [
                    {"id": "1", "name": "Different Twitch title", "igdb_id": "77"}
                ],
                "pagination": {},
            }
        return super().get(endpoint, params=params, **options)


@pytest.mark.parametrize("unavailable", [False, True])
def test_canonical_collection_identity_and_snapshot_keep_census_and_source_documents(
    tmp_path,
    monkeypatch,
    unavailable,
):
    from radar_backend.adapters import twitch_candidates, twitch_snapshot
    from radar_backend.state.validation import (
        validate_persisted_discovery,
        validate_persisted_mapping,
    )

    registry, dates = tmp_path / "registry.json", tmp_path / "dates.json"
    dump(registry, {"schema_version": 1, "observations": {"1": badge()}})
    dump(dates, {"schema_version": 1, "records": {}})
    original_files = {path: path.read_bytes() for path in (registry, dates)}
    catalog = {
        "version": 2,
        "generated_at": STAMP,
        "count": 1,
        "games": [
            {
                "appid": 100,
                "name": "Different Steam title",
                "followers": 5200,
                "release_start": "2026-10-01",
                "release_end": "2026-10-01",
                "release_precision": "day",
                "release_date_timezone": "Asia/Taipei",
            }
        ],
    }
    before = deepcopy(catalog)
    client = IdentityClient(unavailable=unavailable)
    monkeypatch.setattr(twitch_candidates, "datetime", FrozenDateTime)
    payload = twitch_candidates.collect_candidates(
        client_id="fake-client",
        client_secret="fake-secret",
        client=client,
        now=NOW,
        registry_path=registry,
        release_dates_path=dates,
        include_release_hints=False,
        steam_catalog=catalog,
        monotonic=lambda: 0,
    )
    assert payload["coverage"]["collection_complete"] is True
    row = payload["candidate_games"][0]
    assert row["viewer_count"] == 9021 and row["streamer_count"] == 3
    assert row["game_name"] == "Game 1"
    sources = payload["tracking_state"]["games"]["1"]["tracking_sources"]
    assert sources["twitch_new"]["status"] == "active"
    mapping = validate_persisted_mapping(payload["steam_mapping_state"])
    discovery = validate_persisted_discovery(payload["steam_discovery_state"])
    if unavailable:
        assert mapping["games"]["100"]["status"] == "pending"
        assert discovery["games"]["1"]["status"] == "unavailable"
        assert row["steam_matches"] == []
        assert "steam:100" not in sources
    else:
        assert mapping["games"]["100"]["status"] == "matched"
        assert mapping["games"]["100"]["twitch_game_id"] == "1"
        assert discovery["games"]["1"]["steam_appids"] == ["100"]
        assert sources["steam:100"]["status"] == "active"
        assert row["steam_matches"][0]["name"] == "Different Steam title"
    frontend = tmp_path / "frontend"
    frozen_payload = deepcopy(payload)
    relative = twitch_snapshot.store_snapshot(payload, frontend)
    latest = json.loads((frontend / "data/twitch_live.json").read_text())
    hour = json.loads((frontend / relative).read_text())["hours"][STAMP]
    assert (
        latest["candidate_games"][0]["viewer_count"]
        == hour["games"][0]["viewer_count"]
        == 9021
    )
    assert payload == frozen_payload and catalog == before
    assert {path: path.read_bytes() for path in original_files} == original_files
    assert not (frontend / "data/twitch_collection_status.json").exists()


def test_offline_metadata_refresh_uses_canonical_mapping_without_identity_requests(
    tmp_path, monkeypatch
):
    from radar_backend.adapters import steam_twitch_mapping, twitch_snapshot
    from scripts.reconcile_steam_metadata import (
        measurement_projection,
        reconcile_frontend,
    )
    from tests.test_steam_twitch_pipeline import mapping_state
    from tests.test_twitch_history import scheduled_snapshot
    from tests.test_twitch_steam_discovery_pipeline import discovery_state
    from tests.test_twitch_tracking_storage import tracking_state

    payload = scheduled_snapshot()
    payload["tracking_state"] = tracking_state()
    payload["steam_mapping_state"] = mapping_state()
    payload["steam_discovery_state"] = discovery_state()
    twitch_snapshot.store_snapshot(payload, tmp_path)
    catalog = {
        "version": 2,
        "generated_at": STAMP,
        "count": 1,
        "games": [
            {
                "appid": 100,
                "name": "Fresh official Steam title",
                "followers": 6000,
                "release_start": "2026-09-28",
                "release_end": "2026-09-28",
                "release_precision": "day",
                "release_date_timezone": "Asia/Taipei",
            }
        ],
    }
    dump(tmp_path / "data/steam_upcoming.json", catalog)
    original = json.loads((tmp_path / "data/twitch_live.json").read_text())
    untouched = {
        path: path.read_bytes()
        for path in (tmp_path / "data/twitch_history").glob("*.json")
    }
    untouched[tmp_path / "data/twitch_steam_discovery.json"] = (
        tmp_path / "data/twitch_steam_discovery.json"
    ).read_bytes()
    untouched[tmp_path / "data/twitch_collection_status.json"] = (
        tmp_path / "data/twitch_collection_status.json"
    ).read_bytes()
    untouched[tmp_path / "data/steam_upcoming.json"] = (
        tmp_path / "data/steam_upcoming.json"
    ).read_bytes()
    monkeypatch.setattr(
        steam_twitch_mapping,
        "_pages",
        lambda *a, **k: pytest.fail("offline metadata refresh requested an API"),
    )
    report = reconcile_frontend(tmp_path, NOW + timedelta(hours=1))
    updated = json.loads((tmp_path / "data/twitch_live.json").read_text())
    assert report["lookup_count"] == 0 and report["observed_rows_unchanged"] is True
    assert (
        updated["steam_mapping_state"]["games"]["100"]["steam"]["name"]
        == "Fresh official Steam title"
    )
    assert measurement_projection(updated) == measurement_projection(original)
    assert {path: path.read_bytes() for path in untouched} == untouched
