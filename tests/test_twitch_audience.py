from datetime import datetime, timedelta, timezone
import json

import pytest
import requests

from collectors.twitch_audience import FollowerResolver, attach_filtered_audience, filtered_metrics
from collectors.twitch_candidates import collect_candidates
from tests.test_twitch_candidates import FakeClient, game, observed, page, registry, stream

NOW = datetime(2026, 9, 29, 15, tzinfo=timezone.utc)


class Response:
    def __init__(self, payload, status_code=200):
        self.payload, self.status_code = payload, status_code

    def raise_for_status(self):
        if self.status_code != 200:
            raise requests.HTTPError("response contains secret-token-never-log", response=self)

    def json(self):
        return self.payload


class FollowerClient:
    access_token = "secret-token-never-log"
    client_id = "client-id"
    request_interval = 0
    timeout_seconds = 20
    _last_request_at = 0

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.session = self

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item if isinstance(item, Response) else Response({"total": item})


def resolver(tmp_path, responses=(), **kwargs):
    client = FollowerClient(responses)
    return FollowerResolver(client, tmp_path / "followers.json", utcnow=lambda: NOW, **kwargs), client


def save_cache(tmp_path, rows, schema=1):
    (tmp_path / "followers.json").write_text(json.dumps({"schema_version": schema, "followers": rows}))


def record(total, age=timedelta()):
    return {"total": total, "observed_at": (NOW - age).isoformat()}


def test_thresholds_exclude_0_to_9_viewers_and_exactly_1000_followers(tmp_path):
    lookup, client = resolver(tmp_path, [1001, 1000, 1001])
    channels = [stream(1, 1, 0), stream(1, 2, 9), stream(1, 3, 10), stream(1, 4, 20), stream(1, 5, 30)]
    result = filtered_metrics(channels, lookup)
    assert result == {
        "rule": "followers_gt_1000_viewers_gte_10_v1", "min_followers_exclusive": 1000,
        "min_viewers_inclusive": 10, "followers_max_age_hours": 24, "status": "complete",
        "median_viewer_count": 20, "eligible_streamer_count": 2, "eligible_viewer_count": 40,
        "excluded_low_viewer_count": 2, "excluded_low_follower_count": 1, "unknown_follower_count": 0,
    }
    assert [kwargs["params"] for _, kwargs in client.requests] == [{"broadcaster_id": "5"}, {"broadcaster_id": "4"}, {"broadcaster_id": "3"}]
    assert all(url == "https://api.twitch.tv/helix/channels/followers" for url, _ in client.requests)
    assert all(kwargs["timeout"] <= 8 for _, kwargs in client.requests)


def test_odd_sample_uses_median_not_mean(tmp_path):
    lookup, _ = resolver(tmp_path, [1001, 2000, 2000])
    result = filtered_metrics([stream(1, 1, 10), stream(1, 2, 50), stream(1, 3, 10000)], lookup)
    assert result["median_viewer_count"] == 50
    assert result["eligible_viewer_count"] == 10060


def test_unknown_suppresses_partial_median_and_call_budget_keeps_success_cache(tmp_path):
    lookup, client = resolver(tmp_path, [2500], max_calls=1)
    result = filtered_metrics([stream(1, 1, 100), stream(1, 2, 10)], lookup)
    assert result["status"] == "partial" and result["median_viewer_count"] is None
    assert result["eligible_streamer_count"] == 1 and result["unknown_follower_count"] == 1
    assert lookup.stop_reason == "call_budget_exhausted"
    assert lookup.save()
    saved = json.loads((tmp_path / "followers.json").read_text())
    assert saved["followers"]["1"]["total"] == 2500
    assert set(saved["followers"]) == {"1"}
    second, _ = resolver(tmp_path, [3000])
    result = filtered_metrics([stream(1, 1, 100), stream(1, 2, 10)], second)
    assert result["median_viewer_count"] == 55 and second.cache_hits == 1 and second.calls == 1
    assert len(client.requests) == 1


def test_all_excluded_is_complete_empty_sample_not_zero(tmp_path):
    lookup, _ = resolver(tmp_path, [0])
    result = filtered_metrics([stream(1, 1, 9), stream(1, 2, 50)], lookup)
    assert result["status"] == "complete" and result["median_viewer_count"] is None
    assert result["eligible_streamer_count"] == 0
    assert result["excluded_low_viewer_count"] == result["excluded_low_follower_count"] == 1


def test_success_and_failure_results_deduplicated_across_games(tmp_path):
    lookup, client = resolver(tmp_path, [3000, requests.Timeout("secret-token-never-log")])
    first = filtered_metrics([stream(1, 1, 100), stream(1, 2, 20)], lookup)
    second = filtered_metrics([stream(2, 1, 50), stream(2, 2, 10)], lookup)
    assert first["median_viewer_count"] is second["median_viewer_count"] is None
    assert len(client.requests) == 2
    assert first["eligible_viewer_count"] == 100 and second["eligible_viewer_count"] == 50


@pytest.mark.parametrize("bad", [record(True), record(-1), record("1001"), record(1001, timedelta(hours=24)),
                                record(1001, timedelta(seconds=-1)), {"total": 1001, "observed_at": "2026-09-29T15:00:00"}])
def test_invalid_expired_future_and_naive_cache_are_not_accepted(tmp_path, bad):
    save_cache(tmp_path, {"1": bad, "2": record(1234, timedelta(hours=23, minutes=59))})
    lookup, _ = resolver(tmp_path, [999])
    assert lookup.resolve("1") == 999 and lookup.resolve("2") == 1234
    assert lookup.calls == lookup.cache_hits == 1
    assert lookup.save()
    assert json.loads((tmp_path / "followers.json").read_text())["followers"]["1"]["total"] == 999


@pytest.mark.parametrize("code,stop", [(401, "authorization_unavailable"), (403, "authorization_unavailable"), (429, "rate_limited")])
def test_permission_and_rate_limit_stop_without_retries_or_secret_logging(tmp_path, caplog, code, stop):
    lookup, client = resolver(tmp_path, [Response({}, code)])
    result = filtered_metrics([stream(1, 1, 100), stream(1, 2, 90)], lookup)
    assert result["unknown_follower_count"] == 2 and result["median_viewer_count"] is None
    assert lookup.stop_reason == stop and len(client.requests) == 1
    assert "secret-token-never-log" not in caplog.text
    assert lookup.save()
    assert json.loads((tmp_path / "followers.json").read_text())["followers"] == {}


def test_repeated_server_failures_stop_but_cached_totals_still_usable(tmp_path):
    save_cache(tmp_path, {"4": record(2000)})
    lookup, client = resolver(tmp_path, [Response({}, 503)] * 3)
    result = filtered_metrics([stream(1, user, 60 - user * 10) for user in range(1, 6)], lookup)
    assert result["unknown_follower_count"] == 4 and result["eligible_streamer_count"] == 1
    assert lookup.stop_reason == "repeated_lookup_failures" and len(client.requests) == 3


@pytest.mark.parametrize("total", [None, "1234", True, -1, 1000.5])
def test_malformed_api_total_is_unknown_not_zero(tmp_path, total):
    lookup, _ = resolver(tmp_path, [Response({"total": total})])
    result = filtered_metrics([stream(1, 1, 100)], lookup)
    assert result["unknown_follower_count"] == 1 and result["excluded_low_follower_count"] == 0
    assert lookup.save() and json.loads((tmp_path / "followers.json").read_text())["followers"] == {}


def test_elapsed_time_budget_stops_new_requests_and_can_use_cache(tmp_path):
    clock = [0]
    save_cache(tmp_path, {"2": record(2000)})
    lookup, client = resolver(tmp_path, [], max_seconds=1, monotonic=lambda: clock[0])
    clock[0] = 1
    assert lookup.resolve("1") is None
    assert lookup.resolve("2") == 2000
    assert lookup.stop_reason == "time_budget_exhausted" and client.requests == []


def test_same_run_memo_rechecks_24_hour_boundary_and_refreshes(tmp_path):
    save_cache(tmp_path, {"1": record(1500, timedelta(hours=24) - timedelta(seconds=1))})
    clock = [NOW]
    client = FollowerClient([900])
    lookup = FollowerResolver(client, tmp_path / "followers.json", utcnow=lambda: clock[0])
    assert lookup.resolve("1") == 1500
    clock[0] += timedelta(seconds=2)
    assert lookup.resolve("1") == 900 and lookup.calls == 1
    assert lookup.cache["1"]["observed_at"] == "2026-09-29T15:00:02Z"


def test_new_then_date_signal_prioritized_under_budget_without_reordering_output(tmp_path):
    lookup, client = resolver(tmp_path, [2000, 3000], max_calls=2)
    rows = [{"game_id": "1", "viewer_count": 30000, "verification": {"status": "pending"}},
            {"game_id": "2", "viewer_count": 20000, "verification": {"status": "pending"},
             "release_experiment": {"igdb_first_release_date": {"predicted_new": True}}},
            {"game_id": "3", "viewer_count": 10000, "verification": {"status": "new"}}]
    coverage = attach_filtered_audience(rows, {row["game_id"]: [stream(row["game_id"], row["game_id"], row["viewer_count"])] for row in rows}, lookup)
    assert [kwargs["params"]["broadcaster_id"] for _, kwargs in client.requests] == ["3", "2"]
    assert [row["game_id"] for row in rows] == ["1", "2", "3"]
    assert coverage["complete_categories"] == 2 and coverage["partial_categories"] == 1
    assert coverage["cache_saved"] is True


def test_collection_keeps_raw_metrics_no_channel_leak_and_only_queries_final_candidates(tmp_path):
    base = FakeClient([
        ("games/top", page([game(1), game(2), game(3)])),
        ("streams", page([stream(1, 11, 6999)])),
        ("streams", page([stream(2, 21, 7100), stream(2, 22, 9), stream(2, 23, 10)])),
        ("streams", page([stream(3, 31, 9000)])),
    ])
    network = FollowerClient([2000, 5000, 1000])
    for key in ("session", "access_token", "client_id", "timeout_seconds", "request_interval", "_last_request_at"):
        setattr(base, key, getattr(network, key))
    result = collect_candidates(client_id="fixture", client_secret="fixture", client=base, now=NOW,
                                registry_path=registry(tmp_path, {"3": observed("new")}), include_release_hints=False,
                                include_filtered_audience=True, followers_cache_path=tmp_path / "followers.json")
    rows = {row["game_id"]: row for row in result["candidate_games"]}
    assert rows["2"]["viewer_count"] == 7119 and rows["2"]["streamer_count"] == 3
    assert rows["2"]["median_viewer_count"] == 10
    assert rows["2"]["filtered_audience"]["median_viewer_count"] == 7100
    assert [kwargs["params"]["broadcaster_id"] for _, kwargs in network.requests] == ["31", "21", "23"]
    assert result["coverage"]["helix_calls_excluding_retries"] == 7
    assert result["coverage"]["census_helix_calls_excluding_retries"] == 4
    public = json.dumps(result)
    assert '"user_id"' not in public and '"_channels"' not in public and "secret-token-never-log" not in public
    for row in rows.values():
        audience = row["filtered_audience"]
        assert row["streamer_count"] == sum(audience[key] for key in ["eligible_streamer_count", "excluded_low_viewer_count", "excluded_low_follower_count", "unknown_follower_count"])


def test_cli_enables_filter_and_exposes_separate_budgets():
    from scripts.update_twitch import build_parser
    args = build_parser().parse_args([])
    assert args.no_filtered_audience is False
    assert args.followers_max_calls == 1000 and args.followers_max_seconds == 300
    assert args.followers_cache == ".cache/twitch_followers.json"
