"""Snapshot composition preserves callback seams, ordering and historical evidence."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import inspect
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from radar_backend.adapters import twitch_snapshot as canonical
from radar_backend.domain import twitch_snapshot as rules
from radar_backend.jobs import store_twitch_snapshot as canonical_job
from scripts import store_twitch_snapshot as legacy
from tests.test_twitch_history import scheduled_snapshot, snapshot
from tests.test_twitch_tracking_storage import tracking_state


APIS = [pytest.param(canonical, id="canonical"), pytest.param(legacy, id="legacy")]


def audience(*, eligible=1, unknown=0, middle=20):
    return {
        "rule": "followers_gt_1000_viewers_gte_10_v1",
        "min_followers_exclusive": 1000,
        "min_viewers_inclusive": 10,
        "followers_max_age_hours": 24,
        "eligible_streamer_count": eligible,
        "excluded_low_viewer_count": 1,
        "excluded_low_follower_count": 1,
        "unknown_follower_count": unknown,
        "eligible_viewer_count": 20 if eligible else 0,
        "median_viewer_count": middle,
        "status": "partial" if unknown else "complete",
    }


def full_payload():
    payload = scheduled_snapshot()
    payload["tracking_state"] = tracking_state()
    # Empty, dated documents are genuine persisted states, not a response to
    # an unavailable registry. This fixture exercises all write callbacks.
    for field in ("steam_mapping_state", "steam_discovery_state"):
        payload[field] = {
            "schema_version": 1,
            "updated_at": payload["generated_at"],
            "games": {},
        }
    return payload


@pytest.mark.parametrize("api", APIS)
def test_public_signatures_and_constants_remain_compatible(api):
    expected = {
        "merge_discovery_state": "(existing: 'dict | None', incoming: 'dict') -> 'dict'",
        "merge_mapping_state": "(existing: 'dict | None', incoming: 'dict') -> 'dict'",
        "merge_tracking_state": "(existing: 'dict | None', incoming: 'dict') -> 'dict'",
        "observed_rows": "(payload: 'dict') -> 'list[dict]'",
        "validate_schedule": "(payload: 'dict') -> 'dict | None'",
        "observation_order": "(payload: 'dict') -> 'tuple[datetime, datetime]'",
        "validate_receipt": "(receipt: 'dict') -> 'tuple[datetime, datetime]'",
        "validate_filtered_audience": "(value: 'dict', *, viewers: 'int', streamers: 'int') -> 'None'",
        "validate_snapshot": "(payload: 'dict') -> 'None'",
        "store_snapshot": "(payload: 'dict', frontend: 'Path') -> 'str'",
    }
    for name, signature in expected.items():
        assert str(inspect.signature(getattr(api, name))) == signature
    assert api.TAIPEI.utcoffset(None) == timedelta(hours=8)
    assert (
        api.STATUS_PATH,
        api.TRACKING_PATH,
        api.MAPPING_PATH,
        api.DISCOVERY_PATH,
    ) == (
        "data/twitch_collection_status.json",
        "data/twitch_tracking.json",
        "data/twitch_steam_mapping.json",
        "data/twitch_steam_discovery.json",
    )


@pytest.mark.parametrize("api", APIS)
def test_row_order_uses_tracked_reference_without_reordering_candidate_position(api):
    first, second = deepcopy(snapshot()["candidate_games"][0]), deepcopy(
        snapshot()["candidate_games"][0]
    )
    second["game_id"] = "2"
    overlap = deepcopy(first)
    overlap["tracking"] = {"status": "active"}
    third = deepcopy(first)
    third["game_id"] = "3"
    payload = {"candidate_games": [first, second], "tracked_games": [third, overlap]}
    rows = api.observed_rows(payload)
    assert [row["game_id"] for row in rows] == ["1", "2", "3"]
    assert rows[0] is overlap and rows[1] is second and rows[2] is third


@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize(
    "field",
    [
        "viewer_count",
        "streamer_count",
        "median_viewer_count",
        "measurement_started_at",
        "measurement_finished_at",
        "filtered_audience",
    ],
)
def test_conflicting_candidate_and_tracked_metrics_use_current_observation_callback(
    api, field
):
    first = snapshot()["candidate_games"][0]
    overlap = deepcopy(first)
    overlap[field] = "different"
    with pytest.raises(ValueError, match="Candidate and tracked observations disagree"):
        api.observed_rows({"candidate_games": [first], "tracked_games": [overlap]})


@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize(
    "eligible,unknown,middle", [(1, 0, 20), (1, 1, None), (0, 0, None), (0, 1, None)]
)
def test_filtered_population_accepts_real_zero_and_incomplete_coverage(
    api, eligible, unknown, middle
):
    value = audience(eligible=eligible, unknown=unknown, middle=middle)
    assert (
        api.validate_filtered_audience(
            value, viewers=100, streamers=eligible + unknown + 2
        )
        is None
    )


@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize(
    "change",
    [
        {"min_followers_exclusive": True},
        {"eligible_streamer_count": -1},
        {"unknown_follower_count": 2},
        {"eligible_viewer_count": 9},
        {"median_viewer_count": float("nan")},
        {"median_viewer_count": float("inf")},
        {"median_viewer_count": True},
        {"status": "partial"},
    ],
)
def test_filtered_population_rejects_false_median_or_inconsistent_coverage(api, change):
    value = audience()
    value.update(change)
    with pytest.raises(ValueError):
        api.validate_filtered_audience(value, viewers=100, streamers=3)


@pytest.mark.parametrize("api", APIS)
def test_filtered_validation_uses_current_math_callback(api, monkeypatch):
    seen = []
    monkeypatch.setattr(
        api, "math", SimpleNamespace(isfinite=lambda value: seen.append(value) or False)
    )
    with pytest.raises(ValueError, match="Invalid filtered audience median"):
        api.validate_filtered_audience(audience(), viewers=100, streamers=3)
    assert seen == [20]


@pytest.mark.parametrize("api", APIS)
def test_snapshot_validation_and_projection_share_current_observed_rows(
    api, monkeypatch, tmp_path
):
    payload = scheduled_snapshot()
    replacement = deepcopy(payload["candidate_games"][0])
    replacement["game_id"] = "2"
    calls = []
    monkeypatch.setattr(
        api, "observed_rows", lambda value: calls.append(value) or [replacement]
    )
    relative = api.store_snapshot(payload, tmp_path)
    assert calls == [payload, payload]
    history = json.loads((tmp_path / relative).read_text())
    assert history["hours"]["2026-09-28T16:00:00Z"]["games"][0]["game_id"] == "2"


@pytest.mark.parametrize("api", APIS)
def test_validate_snapshot_passes_filtered_counts_to_current_helper(api, monkeypatch):
    payload = snapshot()
    filtered = audience()
    payload["candidate_games"][0]["filtered_audience"] = filtered
    seen = []
    monkeypatch.setattr(
        api,
        "validate_filtered_audience",
        lambda value, **kwargs: seen.append((value, kwargs)),
    )
    api.validate_snapshot(payload)
    assert seen == [(filtered, {"viewers": 10000, "streamers": 5})]
    assert seen[0][0] is filtered


@pytest.mark.parametrize("api", APIS)
def test_validation_failure_occurs_before_path_or_write_access(api, monkeypatch):
    class UnreadableFrontend:
        def __truediv__(self, other):
            pytest.fail("Validation must precede destination IO")

    def fail(payload):
        raise RuntimeError("validation callback failure")

    monkeypatch.setattr(api, "validate_snapshot", fail)
    monkeypatch.setattr(
        api, "write_json", lambda *args: pytest.fail("No writes before validation")
    )
    with pytest.raises(RuntimeError, match="validation callback failure"):
        api.store_snapshot({}, UnreadableFrontend())


@pytest.mark.parametrize("api", APIS)
def test_read_validation_and_write_callbacks_keep_original_order(
    api, monkeypatch, tmp_path
):
    old = full_payload()
    api.store_snapshot(old, tmp_path)
    payload = full_payload()
    payload["generated_at"] = "2026-09-28T17:20:00Z"
    events = []

    class TracedPath:
        def __init__(self, path):
            self.path = path

        def __truediv__(self, relative):
            return TracedPath(self.path / relative)

        def exists(self):
            events.append(("exists", self.path.relative_to(tmp_path).as_posix()))
            return self.path.exists()

        def read_text(self, **kwargs):
            events.append(("read", self.path.relative_to(tmp_path).as_posix()))
            return self.path.read_text(**kwargs)

    for name in (
        "validate_snapshot",
        "validate_receipt",
        "validate_persisted_tracking",
        "validate_persisted_mapping",
        "validate_persisted_discovery",
        "merge_tracking_state",
        "merge_mapping_state",
        "merge_discovery_state",
    ):
        original = getattr(api, name)

        def traced(*args, owner=name, function=original, **kwargs):
            events.append(("callback", owner))
            return function(*args, **kwargs)

        monkeypatch.setattr(api, name, traced)
    writer = api.write_json

    def write(value, path):
        events.append(("write", path.path.relative_to(tmp_path).as_posix()))
        return writer(value, path.path)

    monkeypatch.setattr(api, "write_json", write)
    relative = api.store_snapshot(payload, TracedPath(tmp_path))
    assert relative == "data/twitch_history/2026-09-29.json"
    assert [item[1] for item in events if item[0] == "read"] == [
        relative,
        "data/twitch_live.json",
        "data/twitch_collection_status.json",
        "data/twitch_tracking.json",
        "data/twitch_steam_mapping.json",
        "data/twitch_steam_discovery.json",
    ]
    assert [item[1] for item in events if item[0] == "exists"] == [
        relative,
        "data/twitch_live.json",
        "data/twitch_collection_status.json",
        "data/twitch_tracking.json",
        "data/twitch_tracking.json",
        "data/twitch_steam_mapping.json",
        "data/twitch_steam_mapping.json",
        "data/twitch_steam_discovery.json",
        "data/twitch_steam_discovery.json",
    ]
    # All validation and merge callbacks finish before the first persisted file.
    assert events[0] == ("callback", "validate_snapshot")
    first_write = next(
        index for index, event in enumerate(events) if event[0] == "write"
    )
    assert all(
        event[0] not in {"read", "exists", "callback"} for event in events[first_write:]
    )
    # The old single-source tracking fixture is reconciled when two states
    # are merged; that existing migration remains between latest and receipt.
    assert [item[1] for item in events if item[0] == "write"] == [
        relative,
        "data/twitch_live.json",
        "data/twitch_tracking.json",
        "data/twitch_collection_status.json",
    ]


@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize("fail_at", range(6))
def test_writer_failure_keeps_original_prefix_and_never_claims_receipt_early(
    api, monkeypatch, tmp_path, fail_at
):
    payload = full_payload()
    paths = [
        "data/twitch_history/2026-09-29.json",
        "data/twitch_live.json",
        api.TRACKING_PATH,
        api.MAPPING_PATH,
        api.DISCOVERY_PATH,
        api.STATUS_PATH,
    ]
    calls = []
    writer = api.write_json

    def write(value, path):
        relative = path.relative_to(tmp_path).as_posix()
        calls.append(relative)
        if len(calls) == fail_at + 1:
            raise OSError("destination fixture write failure")
        return writer(value, path)

    monkeypatch.setattr(api, "write_json", write)
    with pytest.raises(OSError, match="destination fixture write failure"):
        api.store_snapshot(payload, tmp_path)
    assert calls == paths[: fail_at + 1]
    assert {
        path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*.json")
    } == set(paths[:fail_at])
    assert not (tmp_path / api.STATUS_PATH).exists()


@pytest.mark.parametrize("api", APIS)
def test_optional_registry_absence_keeps_destinations_absent(
    api, monkeypatch, tmp_path
):
    payload = snapshot()
    for name in (
        "merge_tracking_state",
        "merge_mapping_state",
        "merge_discovery_state",
    ):
        monkeypatch.setattr(
            api, name, lambda *args: pytest.fail("No registry must be invented")
        )
    relative = api.store_snapshot(payload, tmp_path)
    assert {
        path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*.json")
    } == {
        relative,
        "data/twitch_live.json",
    }


@pytest.mark.parametrize("api", APIS)
def test_store_snapshot_passes_current_merge_callbacks_and_path_constants(
    api, monkeypatch, tmp_path
):
    payload = full_payload()
    calls, writes = [], []
    fields = ["tracking_state", "steam_mapping_state", "steam_discovery_state"]
    for name, field in zip(
        ("merge_tracking_state", "merge_mapping_state", "merge_discovery_state"), fields
    ):
        merged = {"fixture": field}

        def merge(old, new, owner=name, expected=payload[field], output=merged):
            calls.append((owner, old, new))
            assert new is expected
            return output

        monkeypatch.setattr(api, name, merge)
    for name in ("TRACKING_PATH", "MAPPING_PATH", "DISCOVERY_PATH", "STATUS_PATH"):
        monkeypatch.setattr(api, name, "custom/" + name.lower() + ".json")
    monkeypatch.setattr(
        api,
        "write_json",
        lambda value, path: writes.append(
            (value, path.relative_to(tmp_path).as_posix())
        ),
    )
    relative = api.store_snapshot(payload, tmp_path)
    assert [value[0] for value in calls] == [
        "merge_tracking_state",
        "merge_mapping_state",
        "merge_discovery_state",
    ]
    assert all(value[1] is None for value in calls)
    assert [path for _, path in writes] == [
        relative,
        "data/twitch_live.json",
        "custom/tracking_path.json",
        "custom/mapping_path.json",
        "custom/discovery_path.json",
        "custom/status_path.json",
    ]
    for position, field in enumerate(fields, start=2):
        assert writes[1][0][field] is writes[position][0]


@pytest.mark.parametrize("api", APIS)
def test_registry_merge_uses_current_validator_and_normalizer_globals(api, monkeypatch):
    old = {"updated_at": "2026-09-28T17:00:00Z", "games": {}}
    new = {"updated_at": "2026-09-29T17:00:00Z", "games": {}}
    calls = []
    monkeypatch.setattr(
        api,
        "validate_persisted_tracking",
        lambda value: calls.append(("validate", value)) or deepcopy(value),
    )
    monkeypatch.setattr(
        api,
        "normalize_tracking_state",
        lambda state, **kwargs: calls.append(("normalize", state, kwargs)) or state,
    )
    result = api.merge_tracking_state(old, new)
    assert calls[0] == ("validate", new) and calls[1] == ("validate", old)
    assert calls[2][0] == "normalize" and calls[2][1] is result
    assert calls[2][2] == {"now": datetime(2026, 9, 29, 17, tzinfo=timezone.utc)}
    assert result == new and result is not new


def test_history_projection_preserves_optional_evidence_and_reference_contract():
    payload = scheduled_snapshot()
    row = payload["candidate_games"][0]
    for name in (
        "release_experiment",
        "filtered_audience",
        "tracking",
        "steam_matches",
    ):
        row[name] = {"original": name}
    projected = rules.history_observation(
        payload, True, observed_rows_fn=canonical.observed_rows
    )
    archived = projected["games"][0]
    assert projected["collection_schedule"] == payload["collection_schedule"]
    assert projected["collection_schedule"] is not payload["collection_schedule"]
    for name in (
        "verification",
        "release_experiment",
        "filtered_audience",
        "tracking",
        "steam_matches",
    ):
        assert archived[name] is row[name]
    assert "pagination_complete" not in archived
    assert "top_games" not in projected


def test_latest_projection_copies_census_but_embeds_merged_registry_objects():
    payload = full_payload()
    merged = [{"merged": "tracking"}, {"merged": "mapping"}, {"merged": "discovery"}]
    before = deepcopy(payload)
    projected = rules.latest_observation(payload, *merged, deepcopy_fn=deepcopy)
    assert projected["candidate_games"] == payload["candidate_games"]
    assert projected["candidate_games"] is not payload["candidate_games"]
    assert projected["candidate_games"][0] is not payload["candidate_games"][0]
    for field, value in zip(
        ("tracking_state", "steam_mapping_state", "steam_discovery_state"), merged
    ):
        assert projected[field] is value
    assert payload == before


def test_latest_projection_does_not_introduce_optional_registries():
    payload = snapshot()
    projected = rules.latest_observation(payload, {}, {}, {}, deepcopy_fn=deepcopy)
    assert projected == payload
    assert not {
        "tracking_state",
        "steam_mapping_state",
        "steam_discovery_state",
    }.intersection(projected)


@pytest.mark.parametrize(
    "job",
    [pytest.param(canonical_job, id="canonical"), pytest.param(legacy, id="legacy")],
)
def test_job_main_uses_current_store_function_and_real_json_input(
    job, monkeypatch, capsys, tmp_path
):
    source = tmp_path / "snapshot.json"
    payload = snapshot()
    source.write_text(json.dumps(payload), encoding="utf-8")
    destination = tmp_path / "frontend"
    received = []
    monkeypatch.setattr(
        job,
        "store_snapshot",
        lambda value, path: received.append((value, path)) or "fixture/history.json",
    )
    monkeypatch.setattr(sys, "argv", ["snapshot", str(source), str(destination)])
    job.main()
    assert received == [(payload, destination)]
    assert capsys.readouterr().out == "fixture/history.json\n"
    assert not destination.exists()


@pytest.mark.parametrize(
    "module",
    ["radar_backend.jobs.store_twitch_snapshot", "scripts.store_twitch_snapshot"],
)
def test_snapshot_cli_help_requires_no_credentials(module):
    process = subprocess.run(
        [sys.executable, "-m", module, "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "snapshot frontend" in process.stdout
    assert not process.stderr


def test_canonical_snapshot_runs_with_legacy_modules_blocked(tmp_path):
    source = tmp_path / "snapshot.json"
    source.write_text(json.dumps(full_payload()), encoding="utf-8")
    destination = tmp_path / "frontend"
    code = """
import importlib.abc
import sys

blocked = {'collectors.twitch_candidates', 'collectors.twitch_audience',
           'collectors.twitch_tracking', 'collectors.twitch_newness', 'collectors.twitch_live',
           'collectors.steam_twitch_mapping', 'collectors.twitch_steam_discovery',
           'collectors.twitch_steam_website_identity', 'scripts.store_twitch_snapshot',
           'scripts.load_twitch_tracking'}
class BlockLegacy(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in blocked:
            raise AssertionError('Legacy owner imported: ' + fullname)
sys.meta_path.insert(0, BlockLegacy())
from radar_backend.jobs.store_twitch_snapshot import main
sys.argv = ['snapshot', sys.argv[1], sys.argv[2]]
main()
assert not blocked.intersection(sys.modules)
"""
    process = subprocess.run(
        [sys.executable, "-c", code, str(source), str(destination)],
        capture_output=True,
        text=True,
        check=True,
    )
    relative = "data/twitch_history/2026-09-29.json"
    assert process.stdout == relative + "\n"
    assert not process.stderr
    latest = json.loads((destination / "data/twitch_live.json").read_text())
    receipt = json.loads((destination / canonical.STATUS_PATH).read_text())
    assert latest["coverage"]["collection_complete"] is True
    assert receipt["history_path"] == relative
    assert receipt["observed_slot"] == "2026-09-28T16:00:00Z"
