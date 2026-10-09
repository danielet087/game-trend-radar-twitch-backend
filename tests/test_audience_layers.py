"""Offline contracts for the canonical audience composition and explicit ports."""

from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from radar_backend.adapters import twitch_audience as audience
from radar_backend.application import twitch_audience as application
from radar_backend.domain import twitch_audience as rules
from radar_backend.domain.twitch_newness import parse_timestamp, timestamp
from radar_backend.state import twitch_followers as cache

NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)


class Response:
    def __init__(self, total, status=200):
        self.total, self.status_code = total, status

    def raise_for_status(self):
        if self.status_code != 200:
            raise requests.HTTPError("credential-must-not-appear")

    def json(self):
        return {"total": self.total}


class Client:
    access_token = "private-token"
    client_id = "fixture"
    request_interval = 0
    timeout_seconds = 20
    _last_request_at = 0

    def __init__(self, responses=()):
        self.responses = list(responses)
        self.requests = []
        self.session = self

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item if isinstance(item, Response) else Response(item)


def lookup(tmp_path, responses=(), **kwargs):
    client = Client(responses)
    resolver = audience.FollowerResolver(
        client, tmp_path / "followers.json", utcnow=lambda: NOW, **kwargs
    )
    return resolver, client


def record(total=2000, age=timedelta()):
    return {"total": total, "observed_at": timestamp(NOW - age)}


def stream(user=1, viewers=10):
    return {"user_id": str(user), "viewer_count": viewers}


@pytest.mark.parametrize(
    "age,valid",
    [
        (timedelta(), True),
        (timedelta(hours=24) - timedelta(seconds=1), True),
        (timedelta(hours=24), False),
        (timedelta(seconds=-1), False),
    ],
)
def test_freshness_uses_observation_age_with_exclusive_24_hour_end(age, valid):
    row = record(age=age)
    original = dict(row)
    actual = rules._fresh_record(
        row, NOW, parse_timestamp=parse_timestamp, timestamp=timestamp, max_age=rules.MAX_AGE
    )
    assert (actual is not None) is valid
    assert row == original


@pytest.mark.parametrize("total", [None, True, False, -1, "2000", 2000.0])
def test_follower_totals_require_non_negative_exact_integer(total):
    assert audience._fresh_record(record(total), NOW) is None


@pytest.mark.parametrize("observed_at", [None, "", "invalid", "2026-10-09T12:00:00"])
def test_freshness_rejects_missing_invalid_and_naive_observations(observed_at):
    assert audience._fresh_record({"total": 2000, "observed_at": observed_at}, NOW) is None


@pytest.mark.parametrize("viewers", [9, 10, 100])
@pytest.mark.parametrize("followers", [None, 0, 1000, 1001])
def test_metrics_preserve_viewer_and_follower_boundaries(viewers, followers):
    calls = []
    resolver = SimpleNamespace(resolve=lambda user: calls.append(user) or followers)
    metrics = audience.filtered_metrics([stream(viewers=viewers)], resolver)
    eligible = viewers >= 10 and followers is not None and followers > 1000
    unknown = viewers >= 10 and followers is None
    assert calls == ([] if viewers < 10 else ["1"])
    assert metrics["eligible_viewer_count"] == (viewers if eligible else 0)
    assert metrics["median_viewer_count"] == (viewers if eligible else None)
    assert metrics["unknown_follower_count"] == int(unknown)
    assert metrics["status"] == ("partial" if unknown else "complete")


@pytest.mark.parametrize("max_calls", [True, -1, 1.0, "1"])
def test_invalid_call_budget_is_rejected_before_cache_read(tmp_path, monkeypatch, max_calls):
    monkeypatch.setattr(audience._cache, "load", lambda *a, **kw: pytest.fail("cache read"))
    with pytest.raises(ValueError, match="non-negative integer"):
        lookup(tmp_path, max_calls=max_calls)


@pytest.mark.parametrize("max_seconds", [0, -1, math.inf, -math.inf, math.nan])
def test_invalid_time_budget_is_rejected(tmp_path, max_seconds):
    with pytest.raises(ValueError, match="positive and finite"):
        lookup(tmp_path, max_seconds=max_seconds)


@pytest.mark.parametrize("deadline", [math.inf, -math.inf, math.nan])
def test_invalid_collection_deadline_is_rejected(tmp_path, deadline):
    with pytest.raises(ValueError, match="deadline must be finite"):
        lookup(tmp_path, collection_deadline=deadline)


@pytest.mark.parametrize(
    "status,reason",
    [
        (401, "authorization_unavailable"),
        (403, "authorization_unavailable"),
        (429, "rate_limited"),
    ],
)
def test_permission_and_rate_limit_failures_stop_without_retry_or_credentials(
    tmp_path, caplog, status, reason
):
    resolver, client = lookup(tmp_path, [Response(None, status)])
    assert resolver.resolve("1") is None
    assert resolver.resolve("1") is None
    assert resolver.resolve("2") is None
    assert resolver.failures == resolver.calls == 1
    assert resolver.stop_reason == reason and len(client.requests) == 1
    assert "private-token" not in caplog.text
    assert "credential-must-not-appear" not in caplog.text
    assert resolver.save()
    assert json.loads(resolver.cache_path.read_text())["followers"] == {}


def test_cache_is_available_after_budget_exhaustion_and_ids_stay_local(tmp_path):
    (tmp_path / "followers.json").write_text(
        json.dumps({"schema_version": 1, "followers": {"2": record(), "٣": record()}})
    )
    resolver, client = lookup(tmp_path, [2500], max_calls=1)
    metrics = audience.filtered_metrics([stream(1, 100), stream(3, 50), stream(2, 10)], resolver)
    assert metrics["status"] == "partial" and metrics["median_viewer_count"] is None
    assert resolver.cache_hits == 1 and resolver.calls == 1
    assert len(client.requests) == 1 and "٣" not in resolver.cache
    assert '"user_id"' not in json.dumps(metrics)
    assert resolver.save()
    assert set(json.loads(resolver.cache_path.read_text())["followers"]) == {"1", "2"}


def test_http_receives_only_remaining_shared_deadline_and_updates_pacing_in_finally(tmp_path):
    clock = [8.0]
    resolver, client = lookup(tmp_path, [2001], collection_deadline=10, monotonic=lambda: clock[0])
    original_get = client.get

    def get(*args, **kwargs):
        clock[0] = 10
        return original_get(*args, **kwargs)

    client.get = get
    assert resolver.resolve("1") == 2001
    url, kwargs = client.requests[0]
    assert url == "https://api.twitch.tv/helix/channels/followers"
    assert kwargs["params"] == {"broadcaster_id": "1"}
    assert kwargs["timeout"].total == 2
    assert kwargs["timeout"].connect_timeout == 2
    assert kwargs["timeout"].read_timeout == 2
    assert client._last_request_at == 10
    assert resolver.resolve("2") is None
    assert resolver.stop_reason == "collection_deadline_exhausted"


def test_deadline_tie_preserves_collection_reason_and_skips_sleep(tmp_path):
    clock = [0.0]
    sleeps = []
    resolver, client = lookup(
        tmp_path,
        [],
        max_seconds=10,
        collection_deadline=10,
        monotonic=lambda: clock[0],
        sleep=sleeps.append,
    )
    clock[0] = 9.8
    client.request_interval, client._last_request_at = 1, 9.8
    assert resolver.resolve("1") is None
    assert resolver.stop_reason == "collection_deadline_exhausted"
    assert sleeps == [] and client.requests == []


def test_success_after_failure_resets_streak_and_checkpoint_50_keeps_fresh_successes(tmp_path):
    resolver, client = lookup(
        tmp_path, [requests.Timeout("private-token")] + [2001] * 50, monotonic=lambda: 5
    )
    assert resolver.resolve("1") is None
    for user in range(2, 52):
        assert resolver.resolve(str(user)) == 2001
    assert resolver.failures == 1 and resolver.consecutive_failures == 0
    assert resolver.successes == 50 and resolver.checkpoints_saved == 1
    saved = json.loads(resolver.cache_path.read_text())
    assert len(saved["followers"]) == 50 and "1" not in saved["followers"]
    assert saved["schema_version"] == 1 and client._last_request_at == 5


def test_atomic_replace_failure_keeps_old_cache_and_removes_temporary(tmp_path, monkeypatch):
    resolver, _ = lookup(tmp_path, [2001])
    assert resolver.resolve("1") == 2001
    resolver.cache_path.write_text("old content")
    attempts = []

    def fail_replace(source, target):
        attempts.append((Path(source), Path(target)))
        raise OSError("private-token")

    monkeypatch.setattr(audience.os, "replace", fail_replace)
    assert resolver.save() is False
    assert resolver.cache_path.read_text() == "old content"
    assert len(attempts) == 1 and not attempts[0][0].exists()
    assert list(tmp_path.glob(".followers-*.tmp")) == []


def test_application_can_run_with_all_time_transport_and_cache_ports_in_memory():
    writes, calls = [], []
    resolver = application.FollowerResolver(
        Client(),
        "memory",
        max_calls=1,
        utcnow=lambda: NOW,
        monotonic=lambda: 0,
        sleep=lambda seconds: pytest.fail("sleep"),
        path_type=lambda value: value,
        load_cache=lambda path: {"followers": {"2": record()}},
        save_cache=lambda path, rows: writes.append((path, rows)) or True,
        fresh_record=lambda row, now: audience._fresh_record(row, now),
        timestamp=timestamp,
        request=lambda client, user, remaining: calls.append((user, remaining)) or Response(3000),
        request_exception=lambda: requests.RequestException,
        warning=lambda message: pytest.fail(message),
        math_module=lambda: math,
    )
    assert resolver.resolve("1") == 3000 and resolver.resolve("2") == 2000
    assert resolver.resolve("3") is None and resolver.stop_reason == "call_budget_exhausted"
    assert calls == [("1", math.inf)]
    assert resolver.save() is True
    assert writes == [("memory", {"2": record(), "1": record(3000)})]


def test_priority_changes_lookup_order_without_reordering_candidates_and_saves(tmp_path):
    resolver, client = lookup(tmp_path, [2000, 3000], max_calls=2)
    rows = [
        {"game_id": "1", "viewer_count": 30000, "verification": {"status": "pending"}},
        {
            "game_id": "2",
            "viewer_count": 20000,
            "release_experiment": {"igdb": {"predicted_new": True}},
        },
        {"game_id": "3", "viewer_count": 10000, "verification": {"status": "new"}},
    ]
    summary = audience.attach_filtered_audience(
        rows, {r["game_id"]: [stream(r["game_id"], r["viewer_count"])] for r in rows}, resolver
    )
    assert [kw["params"]["broadcaster_id"] for _, kw in client.requests] == ["3", "2"]
    assert [row["game_id"] for row in rows] == ["1", "2", "3"]
    assert summary["complete_categories"] == 2 and summary["partial_categories"] == 1
    assert summary["cache_saved"] is True
    assert set(json.loads(resolver.cache_path.read_text())["followers"]) == {"2", "3"}


def test_attach_always_saves_and_propagates_unexpected_metric_exception(tmp_path, monkeypatch):
    resolver, _ = lookup(tmp_path)
    saves = []
    monkeypatch.setattr(resolver, "save", lambda: saves.append(True))

    def metrics(*args):
        raise RuntimeError("fixture")

    monkeypatch.setattr(audience, "filtered_metrics", metrics)
    with pytest.raises(RuntimeError, match="fixture"):
        audience.attach_filtered_audience(
            [{"game_id": "1", "viewer_count": 10}], {"1": []}, resolver
        )
    assert saves == [True]


@pytest.mark.parametrize(
    "payload",
    [None, [], {}, {"schema_version": 2, "followers": {}}, {"schema_version": 1, "followers": []}],
)
def test_cache_shape_validation_is_owned_by_state(tmp_path, payload):
    path = tmp_path / "cache.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="Invalid follower cache"):
        cache.load(path, json_module=json)


def test_legacy_resolver_uses_patched_runtime_helpers_after_construction(tmp_path, monkeypatch):
    from collectors import twitch_audience as legacy

    client = Client([2001])
    resolver = legacy.FollowerResolver(client, tmp_path / "legacy.json", utcnow=lambda: NOW)
    seen = []
    monkeypatch.setattr(legacy, "_fresh_record", lambda row, now: seen.append((row, now)) or None)
    monkeypatch.setattr(legacy, "timestamp", lambda value: "patched timestamp")
    assert resolver.resolve("1") == 2001
    assert resolver.cache["1"]["observed_at"] == "patched timestamp"
    assert seen == [(None, NOW)]


def test_legacy_filtered_metrics_reads_threshold_after_resolver_changes_it(monkeypatch):
    from collectors import twitch_audience as legacy

    def resolve(user):
        monkeypatch.setattr(legacy, "MIN_FOLLOWERS", 3000)
        return 2000

    metrics = legacy.filtered_metrics([stream(1, 10)], SimpleNamespace(resolve=resolve))
    assert metrics["excluded_low_follower_count"] == 1
    assert metrics["min_followers_exclusive"] == 3000
