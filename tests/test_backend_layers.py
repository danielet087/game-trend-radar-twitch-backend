"""Offline checks for collection boundaries, legacy entry points and failures."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from radar_backend.application.collect_twitch import collect_observations
from radar_backend.domain.collection import CollectionInputs, CollectionRequest, TwitchCredentials
from radar_backend.jobs.twitch import run_collection_job
from radar_backend.jobs.twitch_cli import build_parser


def request(*args):
    return CollectionRequest(**vars(build_parser().parse_args(list(args))))


class MemoryInputs:
    def __init__(self, bundle=CollectionInputs()):
        self.bundle = bundle
        self.requests = []

    def load(self, collection_request):
        self.requests.append(collection_request)
        return self.bundle


def test_application_accepts_input_store_and_collector_without_process_environment():
    options = request("--target-slot", "2026-10-08T13:00:00Z", "--no-release-hints",
                      "--followers-max-calls", "0", "--followers-max-seconds", "0")
    bundle = CollectionInputs({"fixture": "tracking"}, {"fixture": "catalog"},
                              {"fixture": "mapping"}, {"fixture": "discovery"})
    store = MemoryInputs(bundle)
    calls = []
    measurement = {"coverage": {"collection_complete": True}, "observed_at": "real-sample-time"}

    def collector(**kwargs):
        calls.append(kwargs)
        return measurement

    result = collect_observations(options, TwitchCredentials("id", "secret"),
                                  input_store=store, collector=collector,
                                  run_id="123", run_attempt="2")
    assert store.requests == [options]
    assert calls == [{
        "client_id": "id", "client_secret": "secret", "min_viewers": 7000,
        "max_category_pages": 5, "max_stream_pages": 150, "max_api_calls": 1200,
        "max_collection_seconds": 1500, "registry_path": options.verification_registry,
        "release_dates_path": options.release_dates, "include_release_hints": False,
        "include_filtered_audience": True, "followers_cache_path": options.followers_cache,
        "followers_max_calls": 0, "followers_max_seconds": 0,
        "tracking_state": bundle.tracking_state, "steam_catalog": bundle.steam_catalog,
        "steam_mapping_state": bundle.steam_mapping_state,
        "steam_discovery_state": bundle.steam_discovery_state,
    }]
    assert result["observed_at"] == "real-sample-time"
    assert result["collection_schedule"] == {
        "target_slot": "2026-10-08T13:00:00Z", "trigger_source": "manual",
        "run_id": "123", "run_attempt": "2",
    }


def test_manual_application_does_not_add_schedule_or_success_receipt():
    payload = {"coverage": {"collection_complete": True}, "missing_measurement": None}
    result = collect_observations(request(), TwitchCredentials("id", "secret"),
                                  input_store=MemoryInputs(), collector=lambda **kwargs: payload)
    assert result == {"coverage": {"collection_complete": True}, "missing_measurement": None}
    assert "collection_schedule" not in result
    assert "job_result" not in result


@pytest.mark.parametrize("document", ["null", "{", '{"schema_version": 999}'])
def test_invalid_persisted_input_stops_before_collection_and_snapshot(tmp_path, document):
    state = tmp_path / "tracking.json"
    state.write_text(document)
    options = request("--tracking-state", str(state))
    forbidden = lambda *args, **kwargs: pytest.fail("invalid persisted state must stop all downstream IO")
    with pytest.raises(ValueError):
        run_collection_job(options, {"TWITCH_CLIENT_ID": "id", "TWITCH_CLIENT_SECRET": "secret"},
                           collector=forbidden, writer=forbidden, reporter=forbidden)
    assert state.read_text() == document


def test_production_guard_stops_before_loading_inputs():
    forbidden = lambda *args, **kwargs: pytest.fail("production input guard must run before IO")
    class ForbiddenInputs:
        load = forbidden

    with pytest.raises(SystemExit, match="tracking-state"):
        run_collection_job(request(), {"TWITCH_CLIENT_ID": "id", "TWITCH_CLIENT_SECRET": "secret",
                                       "GITHUB_RUN_ID": "123"},
                           input_store=ForbiddenInputs(), collector=forbidden,
                           writer=forbidden, reporter=forbidden)


def test_credential_guard_precedes_missing_input_file():
    options = request("--tracking-state", "/file/that/does/not/exist")
    forbidden = lambda *args, **kwargs: pytest.fail("missing credentials must stop downstream IO")
    with pytest.raises(SystemExit, match="TWITCH_CLIENT_ID"):
        run_collection_job(options, {}, collector=forbidden, writer=forbidden, reporter=forbidden)


def test_collector_failure_never_writes_snapshot_or_report():
    def collector(**kwargs):
        raise RuntimeError("collection interrupted")
    forbidden = lambda *args, **kwargs: pytest.fail("a failed collection has no successful snapshot")
    with pytest.raises(RuntimeError, match="interrupted"):
        run_collection_job(request(), {"TWITCH_CLIENT_ID": "id", "TWITCH_CLIENT_SECRET": "secret"},
                           input_store=MemoryInputs(), collector=collector,
                           writer=forbidden, reporter=forbidden)


def test_snapshot_write_failure_does_not_report_completion():
    def writer(*args):
        raise OSError("disk unavailable")
    with pytest.raises(OSError, match="disk unavailable"):
        run_collection_job(request(), {"TWITCH_CLIENT_ID": "id", "TWITCH_CLIENT_SECRET": "secret"},
                           input_store=MemoryInputs(), collector=lambda **kwargs: {"sample": True},
                           writer=writer, reporter=lambda *args, **kwargs: pytest.fail("snapshot was not saved"))


def test_legacy_cli_uses_patched_collector_and_writer_with_real_summary(tmp_path, monkeypatch):
    from scripts import update_twitch

    trial = {"window_days": 14, "evaluated_candidates": 0, "unknown_candidates": 0,
             "predicted_new_game_ids": [], "upcoming_game_ids": []}
    payload = {
        "candidate_games": [], "top_games": [], "pending_verification": [], "excluded_games": [],
        "newness_experiment": {"twitch_original_release_date": trial,
                               "igdb_first_release_date": {**trial, "window_days": 30}, "reference_checks": []},
        "coverage": {"excluded_by_igdb_date_count": 0, "helix_calls_excluding_retries": 0,
                     "stop_reason": "fixture", "collection_elapsed_seconds": 0.5,
                     "igdb_categories_evaluated": 0, "igdb_categories_unknown": 0},
    }
    calls = []
    output, summary = tmp_path / "output.json", tmp_path / "summary.md"
    summary.write_text("Earlier step\n")
    monkeypatch.setenv("TWITCH_CLIENT_ID", "id")
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", "secret")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    monkeypatch.setattr(sys, "argv", ["collector", "--output", str(output)])
    monkeypatch.setattr(update_twitch, "collect_twitch", lambda **kwargs: payload)
    real_writer = update_twitch.write_json
    def writer(snapshot, path):
        calls.append((snapshot, path))
        return real_writer(snapshot, path)
    monkeypatch.setattr(update_twitch, "write_json", writer)
    update_twitch.main()
    assert calls == [(payload, str(output))]
    assert json.loads(output.read_text()) == payload
    assert output.read_text().endswith("\n")
    assert summary.read_text().startswith("Earlier step\n")
    assert "7,000" in summary.read_text()


def test_http_identity_and_deadline_are_preserved(monkeypatch):
    from collectors import twitch_live
    from radar_backend.adapters import twitch_http
    from radar_backend.domain.twitch import CollectionDeadlineExceeded, TwitchGame

    assert twitch_live.TwitchClient is twitch_http.TwitchClient
    assert twitch_live.TwitchGame is TwitchGame
    assert twitch_live.CollectionDeadlineExceeded is CollectionDeadlineExceeded
    client = twitch_http.TwitchClient("id", "secret", request_interval=0)
    client.access_token = "existing"
    client.collection_deadline = 1
    client.monotonic = lambda: 2
    monkeypatch.setattr(client.session, "get", lambda *args, **kwargs: pytest.fail("no request after deadline"))
    with pytest.raises(CollectionDeadlineExceeded):
        client.get("games")
    assert "secret" not in repr(TwitchCredentials("id", "secret"))


def test_domain_imports_without_network_or_storage_layers():
    root = Path(__file__).resolve().parents[1]
    code = """
import sys
class BlockIOImports:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(('requests', 'collectors', 'scripts', 'radar_backend.adapters',
                                'radar_backend.state', 'radar_backend.jobs')):
            raise AssertionError('Domain imported an IO layer: ' + fullname)
sys.meta_path.insert(0, BlockIOImports())
from radar_backend.domain.collection import CollectionRequest
from radar_backend.domain.twitch import TwitchGame
from radar_backend.domain.time import validate_slot
assert validate_slot('2026-10-08T13:00:00Z') == '2026-10-08T13:00:00Z'
"""
    subprocess.run([sys.executable, "-c", code], cwd=root, check=True, capture_output=True, text=True)


@pytest.mark.parametrize("entry", ["update_twitch", "load_twitch_tracking"])
@pytest.mark.parametrize("mode", ["module", "script"])
def test_historical_entry_help_needs_no_credentials_or_pythonpath(entry, mode, tmp_path):
    import os

    root = Path(__file__).resolve().parents[1]
    environment = {key: value for key, value in os.environ.items()
                   if key not in {"PYTHONPATH", "TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET"}}
    arguments = (["-m", f"scripts.{entry}"] if mode == "module"
                 else [str(root / "scripts" / f"{entry}.py")])
    completed = subprocess.run([sys.executable, *arguments, "--help"],
                               cwd=root if mode == "module" else tmp_path,
                               env=environment, check=True, capture_output=True, text=True)
    assert "usage:" in completed.stdout


def test_guard_script_dependencies_import_from_other_directory_without_running_main(tmp_path):
    import os

    root = Path(__file__).resolve().parents[1]
    path = root / "scripts/collection_guard.py"
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    code = ("import runpy; guard = runpy.run_path(" + repr(str(path)) + ", run_name='offline_import'); "
            "assert guard['validate_slot']('2026-10-08T13:00:00Z') == '2026-10-08T13:00:00Z'")
    subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=environment,
                   check=True, capture_output=True, text=True, timeout=10)
