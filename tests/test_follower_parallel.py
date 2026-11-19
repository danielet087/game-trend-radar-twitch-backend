"""Parallel follower lookups keep the sequential results, budgets and stop reasons."""

from copy import deepcopy
import threading
import time

import pytest

from collectors import twitch_audience as facade
from radar_backend.adapters import twitch_audience as adapter
from radar_backend.jobs.twitch_cli import build_parser
from tests.test_twitch_audience import NOW, Response, record, save_cache
from tests.test_twitch_candidates import stream


class Helix:
    """Thread-safe fake follower endpoint shared by every session."""

    def __init__(self, totals=None, status=200, delay=0.0):
        self.totals, self.status, self.delay = totals or {}, status, delay
        self.lock = threading.Lock()
        self.requests, self.started, self.sessions = [], [], []
        self.active = self.max_active = 0

    def get(self, url, **kwargs):
        user = kwargs["params"]["broadcaster_id"]
        with self.lock:
            self.requests.append(user)
            self.started.append(time.monotonic())
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                time.sleep(self.delay)
            return Response({"total": self.totals.get(user, 2000)}, self.status)
        finally:
            with self.lock:
                self.active -= 1


class Session:
    def __init__(self, helix):
        self.helix, self.closed = helix, False

    def get(self, url, **kwargs):
        assert not self.closed
        return self.helix.get(url, **kwargs)

    def close(self):
        self.closed = True


class SharedOnly:
    """The shared session must stay idle while workers own the lookups."""

    def get(self, url, **kwargs):
        raise AssertionError("parallel lookups must use worker sessions")


class Client:
    access_token = "secret-token-never-log"
    client_id = "client-id"
    request_interval = 0
    timeout_seconds = 20
    _last_request_at = 0

    def __init__(self, session):
        self.session = session


@pytest.fixture
def helix(monkeypatch):
    fake = Helix(delay=0.005)

    def new_session():
        session = Session(fake)
        fake.sessions.append(session)
        return session

    monkeypatch.setattr(facade.requests, "Session", new_session)
    return fake


def resolver(tmp_path, session, name="followers.json", **kwargs):
    kwargs.setdefault("utcnow", lambda: NOW)
    kwargs.setdefault("parallel_start_spacing", 0)
    return facade.FollowerResolver(Client(session), tmp_path / name, **kwargs)


def census():
    rows = [
        {"game_id": "1", "viewer_count": 30000, "verification": {"status": "pending"}},
        {"game_id": "2", "viewer_count": 20000, "verification": {"status": "new"},
         "audience_regions": {"zh": {"viewer_count": 37, "streamer_count": 2}}},
        {"game_id": "3", "viewer_count": 10000, "verification": {"status": "pending"}},
    ]
    channels = {
        "1": [stream(1, 100 + n, 5 + n) for n in range(40)],
        "2": [stream(2, 200 + n, 10 + 3 * n) for n in range(25)],
        "3": [stream(3, 300, 9)],
    }
    regional = {"2": [stream(2, 205, 25), stream(2, 290, 12)]}
    return rows, channels, regional


def totals():
    return {str(user): (1500 if user % 2 == 0 else 800) for user in range(100, 300)}


def run(lookup, rows, channels, regional):
    rows = deepcopy(rows)
    coverage = facade.attach_filtered_audience(rows, channels, lookup, regional_channels_by_game=regional)
    for key in ("lookup_elapsed_seconds", "lookup_concurrency"):
        coverage.pop(key)
    return rows, coverage


def test_parallel_lookups_match_sequential_results_once_per_broadcaster(tmp_path, helix):
    rows, channels, regional = census()
    helix.totals = totals()
    save_cache(tmp_path, {"110": record(5000)})
    (tmp_path / "parallel.json").write_text((tmp_path / "followers.json").read_text())

    sequential = Helix(totals())
    expected_rows, expected = run(resolver(tmp_path, Session(sequential)), rows, channels, regional)
    parallel = resolver(tmp_path, SharedOnly(), "parallel.json", concurrency=4)
    actual_rows, actual = run(parallel, rows, channels, regional)

    assert actual_rows == expected_rows and actual == expected
    assert sorted(helix.requests) == sorted(sequential.requests)
    assert len(helix.requests) == len(set(helix.requests)) == 60
    assert expected["follower_cache_hits"] == 1 and expected["follower_lookup_calls"] == 60
    assert helix.max_active > 1
    assert 1 < len(helix.sessions) <= 4 and all(session.closed for session in helix.sessions)
    assert parallel.cache["290"]["total"] == 1500


def test_parallel_prefetch_keeps_the_call_budget_across_workers(tmp_path, helix):
    rows, channels, regional = census()
    lookup = resolver(tmp_path, SharedOnly(), concurrency=4, max_calls=5)
    _, coverage = run(lookup, rows, channels, regional)
    assert len(helix.requests) == 5 and lookup.calls == 5
    assert coverage["stop_reason"] == "call_budget_exhausted"
    assert coverage["complete_categories"] == 1  # only the category with no lookups


def test_parallel_prefetch_stops_every_worker_after_a_rate_limit(tmp_path, helix, caplog):
    helix.status = 429
    rows, channels, regional = census()
    lookup = resolver(tmp_path, SharedOnly(), concurrency=4)
    _, coverage = run(lookup, rows, channels, regional)
    assert coverage["stop_reason"] == "rate_limited"
    assert 1 <= len(helix.requests) <= 4
    assert "secret-token-never-log" not in caplog.text


def test_parallel_prefetch_honors_the_shared_collection_deadline(tmp_path, helix):
    rows, channels, regional = census()
    lookup = resolver(tmp_path, SharedOnly(), concurrency=4, collection_deadline=time.monotonic() - 1)
    _, coverage = run(lookup, rows, channels, regional)
    assert helix.requests == []
    assert coverage["stop_reason"] == "collection_deadline_exhausted"


def test_parallel_time_budget_stops_new_requests(tmp_path, helix):
    rows, channels, regional = census()
    lookup = resolver(tmp_path, SharedOnly(), concurrency=4, max_seconds=0.2, parallel_start_spacing=0.05)
    _, coverage = run(lookup, rows, channels, regional)
    assert 0 < len(helix.requests) <= 6
    assert coverage["stop_reason"] == "time_budget_exhausted"


def test_parallel_request_starts_are_spaced_globally(tmp_path, helix):
    lookup = resolver(tmp_path, SharedOnly(), concurrency=4, parallel_start_spacing=0.03)
    assert lookup.prefetch([str(user) for user in range(100, 111)]) == 11
    assert len(helix.requests) == 11
    assert max(helix.started) - min(helix.started) >= 0.25


def test_sequential_resolver_does_not_prefetch(tmp_path, helix):
    lookup = resolver(tmp_path, SharedOnly())
    assert lookup.prefetch(["100", "101"]) == 0 and helix.requests == []


@pytest.mark.parametrize("value", [0, 9, True, 2.0])
def test_concurrency_must_be_a_small_integer(tmp_path, value):
    with pytest.raises(ValueError):
        resolver(tmp_path, SharedOnly(), concurrency=value)


def test_canonical_adapter_offers_the_same_parallel_composition(tmp_path, monkeypatch):
    fake = Helix()
    monkeypatch.setattr(adapter.requests, "Session", lambda: Session(fake))
    lookup = adapter.FollowerResolver(Client(SharedOnly()), tmp_path / "followers.json",
                                      utcnow=lambda: NOW, concurrency=2, parallel_start_spacing=0)
    assert lookup.prefetch(["100", "101", "102"]) == 3
    assert sorted(fake.requests) == ["100", "101", "102"]
    assert lookup.resolve("101") == 2000 and lookup.calls == 3


def test_cli_parses_follower_concurrency():
    parser = build_parser()
    assert parser.parse_args([]).followers_concurrency == 1
    assert parser.parse_args(["--followers-concurrency", "4"]).followers_concurrency == 4
    for value in ("0", "9", "two"):
        with pytest.raises(SystemExit):
            parser.parse_args(["--followers-concurrency", value])
