"""Exercise canonical collection and input wiring with temporary files and fake APIs."""

from copy import deepcopy
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
STAMP = "2026-10-09T12:00:00Z"
COMMIT = "a" * 40


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz)


def dump(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def badge():
    return {
        "status": "new",
        "source": "twitch_directory_dom",
        "source_url": "https://www.twitch.tv/directory/category/game-1",
        "observed_at": "2026-10-09T11:00:00Z",
        "expires_at": "2026-10-10T11:00:00Z",
    }


def prior_tracking():
    from radar_backend.adapters.twitch_tracking import enroll_observation

    state = {"schema_version": 1, "updated_at": None, "games": {}}
    enroll_observation(
        state,
        {
            "game_id": "2",
            "game_name": "Outside directory",
            "viewer_count": 8000,
            "verification": badge(),
        },
        NOW,
    )
    return state


def test_formal_import_graph_runs_without_migrated_legacy_helpers():
    code = """
import importlib
import importlib.abc
import sys
blocked = {'collectors.twitch_candidates', 'collectors.twitch_audience',
           'collectors.twitch_tracking', 'collectors.twitch_newness',
           'collectors.twitch_live', 'scripts.load_twitch_tracking'}
class BlockMigrated(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in blocked:
            raise AssertionError('migrated legacy dependency: ' + fullname)
sys.meta_path.insert(0, BlockMigrated())
for name in ('radar_backend.jobs.collect_twitch', 'radar_backend.jobs.load_frontend_inputs',
             'radar_backend.adapters.frontend_inputs', 'radar_backend.publication.twitch',
             'scripts.update_twitch', 'scripts.store_twitch_snapshot',
             'scripts.recover_twitch_tracking', 'scripts.reconcile_steam_metadata',
             'scripts.import_twitch_release_dates', 'scripts.probe_igdb_hypes'):
    importlib.import_module(name)
from radar_backend.jobs import collect_twitch, load_frontend_inputs
from radar_backend.adapters import twitch_candidates, frontend_inputs
assert collect_twitch.collect_twitch is twitch_candidates.collect_candidates
assert load_frontend_inputs.load_published_inputs is frontend_inputs.load_published_inputs
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


class FollowerResponse:
    status_code = 200

    def __init__(self, user_id):
        self.user_id = user_id

    def raise_for_status(self):
        pass

    def json(self):
        return {"total": 1001 if self.user_id == "101" else 1000}


class FakeClient:
    def __init__(self, *, incomplete=False):
        self.client_id, self.access_token = "fake-client", "fake-token"
        self.request_interval, self.timeout_seconds, self._last_request_at = 0, 20, 0
        self.session = self
        self.incomplete = incomplete
        self.calls = []

    def get(self, endpoint, *, params, **options):
        self.calls.append((endpoint, deepcopy(params), options))
        if endpoint.endswith("/channels/followers"):
            return FollowerResponse(params["broadcaster_id"])
        if endpoint == "games/top":
            return {
                "data": [
                    {"id": "1", "name": "Game 1", "igdb_id": "77"},
                    {"id": "509658", "name": "Just Chatting"},
                    {"id": "1", "name": "Game 1", "igdb_id": "77"},
                ],
                "pagination": {},
            }
        if endpoint == "games":
            return {"data": [], "pagination": {}}
        assert endpoint == "streams"
        game_id = params["game_id"]
        channels = [
            {
                "game_id": game_id,
                "user_id": user_id,
                "viewer_count": count,
                "type": "offline" if self.incomplete else "live",
                "language": "zh",
            }
            for user_id, count in [("101", 8000), ("102", 12), ("103", 9), ("101", 9000)]
        ]
        return {"data": channels if game_id == "1" else [], "pagination": {}}


@pytest.mark.parametrize("incomplete", [False, True])
def test_collection_job_uses_real_census_audience_tracking_and_writes_only_complete_output(
    tmp_path, monkeypatch, incomplete
):
    from radar_backend.adapters import twitch_candidates
    from radar_backend.domain.collection import CollectionInputs, CollectionRequest
    from radar_backend.jobs import collect_twitch
    from radar_backend.jobs.twitch import run_collection_job
    from radar_backend.state.json_snapshot import write_json
    from radar_core.publication import snapshot_revision

    registry, dates = tmp_path / "registry.json", tmp_path / "dates.json"
    dump(registry, {"schema_version": 1, "observations": {"1": badge()}})
    dump(dates, {"schema_version": 1, "records": {}})
    before_files = {path: path.read_bytes() for path in (registry, dates)}
    output, cache = tmp_path / "result.json", tmp_path / "followers.json"
    request = CollectionRequest(
        **vars(
            collect_twitch.build_parser().parse_args(
                [
                    "--output",
                    str(output),
                    "--verification-registry",
                    str(registry),
                    "--release-dates",
                    str(dates),
                    "--followers-cache",
                    str(cache),
                    "--no-release-hints",
                    "--target-slot",
                    STAMP,
                ]
            )
        )
    )
    tracking = prior_tracking()
    original_tracking = deepcopy(tracking)
    inputs = CollectionInputs(tracking_state=tracking)
    store = SimpleNamespace(load=lambda request: inputs)
    client = FakeClient(incomplete=incomplete)
    monkeypatch.setattr(twitch_candidates, "datetime", FrozenDateTime)
    collector = lambda **kwargs: collect_twitch.collect_twitch(
        **kwargs, client=client, now=NOW, monotonic=lambda: 0.0
    )
    reports = []
    if incomplete:
        with pytest.raises(twitch_candidates.IncompleteCollection, match="Non-live"):
            run_collection_job(
                request,
                {"TWITCH_CLIENT_ID": "id", "TWITCH_CLIENT_SECRET": "secret"},
                collector=collector,
                writer=write_json,
                input_store=store,
                reporter=lambda *args, **kwargs: reports.append(args),
            )
        assert not output.exists() and not cache.exists() and reports == []
    else:
        saved = run_collection_job(
            request,
            {"TWITCH_CLIENT_ID": "id", "TWITCH_CLIENT_SECRET": "secret"},
            collector=collector,
            writer=write_json,
            input_store=store,
            reporter=lambda *args, **kwargs: reports.append(args),
        )
        assert saved == output and len(reports) == 1
        payload = json.loads(output.read_text(encoding="utf-8"))
        assert payload["generated_at"] == payload["collection_started_at"] == STAMP
        assert payload["input_revision"] == snapshot_revision(
            {
                "tracking_state": original_tracking,
                "steam_catalog": None,
                "steam_mapping_state": None,
                "steam_discovery_state": None,
            }
        )
        assert payload["collection_schedule"]["target_slot"] == STAMP
        assert payload["coverage"]["collection_complete"] is True
        assert payload["coverage"]["tracked_outside_discovery_count"] == 1
        one = payload["candidate_games"][0]
        assert (one["viewer_count"], one["streamer_count"], one["median_viewer_count"]) == (
            9021,
            3,
            12,
        )
        assert one["duplicate_broadcasters_removed"] == 1
        assert one["filtered_audience"]["median_viewer_count"] == 9000
        assert one["filtered_audience"]["excluded_low_follower_count"] == 1
        assert one["filtered_audience"]["excluded_low_viewer_count"] == 1
        outside = payload["tracked_games"][1]
        assert outside["game_id"] == "2" and outside["viewer_count"] == 0
        assert outside["pagination_complete"] is True and outside["observation_status"] == "current"
        assert outside["median_viewer_count"] is None
        assert set(payload["tracking_state"]["games"]) == {"1", "2"}
        cached = json.loads(cache.read_text(encoding="utf-8"))
        assert set(cached["followers"]) == {"101", "102"}
        assert all("fake-token" not in path.read_text() for path in (cache, output))
        assert "job_result" not in payload
    assert tracking == original_tracking
    assert {path: path.read_bytes() for path in before_files} == before_files


def test_canonical_frontend_input_transport_pins_all_blobs_to_one_head(tmp_path, monkeypatch):
    from radar_backend.adapters import frontend_inputs

    tracking = prior_tracking()
    documents = {
        "data/twitch_tracking.json": tracking,
        "data/steam_upcoming.json": {"version": 2, "count": 0, "games": [], "generated_at": STAMP},
    }
    trace = []

    def run(arguments, **options):
        trace.append(("head", arguments, options))
        return SimpleNamespace(stdout=COMMIT + "\trefs/heads/main\n")

    def open_url(request, *, timeout):
        trace.append(("blob", request.full_url, request.headers, timeout))
        assert "/" + COMMIT + "/" in request.full_url
        path = request.full_url.split(COMMIT + "/", 1)[1]
        if path not in documents:
            raise HTTPError(request.full_url, 404, "absent bootstrap", {}, None)
        return io.BytesIO(json.dumps(documents[path]).encode())

    monkeypatch.setattr(frontend_inputs, "subprocess", SimpleNamespace(run=run))
    monkeypatch.setattr(frontend_inputs, "urlopen", open_url)
    monkeypatch.setattr(frontend_inputs, "datetime", FrozenDateTime)
    before = deepcopy(documents)
    bundle = frontend_inputs.load_published_inputs()
    assert bundle["source_commit"] == bundle["steam_catalog"]["_source_commit"] == COMMIT
    assert (
        bundle["steam_mapping_state"]
        == bundle["steam_discovery_state"]
        == {"schema_version": 1, "updated_at": None, "games": {}}
    )
    assert len([row for row in trace if row[0] == "head"]) == 1
    assert [row[1].split(COMMIT + "/", 1)[1] for row in trace if row[0] == "blob"] == [
        frontend_inputs.TRACKING_PATH,
        frontend_inputs.STEAM_PATH,
        frontend_inputs.MAPPING_PATH,
        frontend_inputs.DISCOVERY_PATH,
    ]
    assert all(row[3] == 20 for row in trace if row[0] == "blob")
    assert documents == before
