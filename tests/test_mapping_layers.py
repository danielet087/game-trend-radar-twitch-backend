"""Offline contracts for canonical identity rules, transport and runtime ports."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib
import inspect
from types import SimpleNamespace

import pytest
import requests
from urllib3.util import Timeout

from radar_backend.domain import steam_twitch_mapping as rules
from radar_core.domain import twitch_admission as core

NOW = datetime(2026, 10, 1, 8, tzinfo=timezone.utc)


@pytest.fixture(
    params=[
        "collectors.steam_twitch_mapping",
        "radar_backend.adapters.steam_twitch_mapping",
    ]
)
def owner(request):
    return importlib.import_module(request.param)


def catalog(*rows):
    games = list(rows) or [
        {
            "appid": 100,
            "name": "Steam name",
            "followers": 5200,
            "release_start": "2026-09-20",
            "release_end": "2026-09-20",
            "release_precision": "day",
            "release_time_utc": "2026-09-20T15:00:00Z",
            "release_date_timezone": "Asia/Taipei",
            "tags": ["Action", "Action"],
            "genres": ["Indie"],
            "tag_labels_zh_tw": {"Action": "動作", "Other": "unused"},
        }
    ]
    return {
        "version": 2,
        "generated_at": "2026-10-01T08:00:00Z",
        "count": len(games),
        "games": games,
    }


class Client:
    access_token, client_id, timeout_seconds, request_interval = (
        "private-token",
        "fixture",
        20,
        0.1,
    )
    _last_request_at = 1.0

    def __init__(self, response=()):
        self.response = response
        self.calls = []
        self.session = self

    def authenticate(self):
        self.calls.append(("authenticate",))
        self.access_token = "private-token"

    def _wait(self):
        self.calls.append(("wait",))

    def _sleep_with_deadline(self, delay):
        self.calls.append(("sleep", delay))

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return SimpleNamespace(
            raise_for_status=lambda: None, json=lambda: self.response
        )

    def get(self, endpoint, *, params):
        self.calls.append(("get", endpoint, params))
        return {
            "data": [{"id": "300", "igdb_id": "20", "name": "Different Twitch name"}]
        }


@pytest.mark.parametrize(
    "value", [None, True, False, 0, -1, 1.0, "0", "-1", "1.0", "１", " 1", "1 ", [], {}]
)
def test_pure_ids_reject_non_authoritative_identity(value):
    with pytest.raises(ValueError, match="ID must be a positive decimal integer"):
        rules._id(value)


@pytest.mark.parametrize(
    "value,expected",
    [(1, "1"), ("001", "1"), ("9876543210987654321", "9876543210987654321")],
)
def test_pure_ids_canonicalize_decimal_values(value, expected):
    assert rules._id(value) == expected


def test_domain_registry_entrypoint_validates_without_runtime_ports():
    assert rules.normalize_mapping_state(None) == {
        "schema_version": 1,
        "updated_at": None,
        "games": {},
    }
    payload = {"schema_version": 1, "games": {}, "steam_source_id": "00045"}
    clean = rules.normalize_mapping_state(payload)
    assert clean["steam_source_id"] == "45" and clean is not payload
    assert payload["steam_source_id"] == "00045"


def test_domain_catalog_entrypoint_requires_explicit_aware_time():
    assert rules.normalize_steam_catalog(catalog(), NOW)[0]["is_recent"] is True
    with pytest.raises(ValueError, match="Clock must include a timezone"):
        rules.normalize_steam_catalog(catalog(), NOW.replace(tzinfo=None))


def test_core_identity_exports_and_bound_monotonic_default(owner):
    assert owner.has_taiwan_store_date_authority is core.has_taiwan_store_date_authority
    assert owner.is_twitch_qualified is core.is_twitch_qualified
    assert (
        inspect.signature(owner.refresh_mappings).parameters["monotonic"].default
        is owner.time.monotonic
    )


@pytest.mark.parametrize(
    "offset,recent",
    [(-1, False), (0, True), (30 * 86400 - 1, True), (30 * 86400, False)],
)
def test_catalog_keeps_half_open_exact_release_window(owner, offset, recent):
    release = datetime(2026, 9, 20, 15, tzinfo=timezone.utc)
    source = catalog()
    before = deepcopy(source)
    row = owner.normalize_steam_catalog(source, release + timedelta(seconds=offset))[0]
    assert row["is_recent"] is recent and source == before
    assert row["tags"] == ["Action"] and row["tag_labels_zh_tw"] == {"Action": "動作"}


def test_catalog_and_registry_use_current_facade_helpers(owner, monkeypatch):
    observed = []
    original_id = owner._id
    monkeypatch.setattr(
        owner, "_id", lambda value: observed.append(value) or original_id(value)
    )
    monkeypatch.setattr(owner, "_labels", lambda value, allowed: {"port": "current"})
    result = owner.normalize_steam_catalog(catalog(), NOW)
    assert observed == [100]
    assert result[0]["tag_labels_zh_tw"] == {"port": "current"}
    state = {
        "schema_version": 1,
        "games": {
            "100": {"steam_appid": "100", "status": "pending", "steam": result[0]}
        },
    }
    clean = owner.normalize_mapping_state(state)
    assert observed == [100, "100", "100", "100"]
    assert (
        clean == state
        and clean is not state
        and clean["games"]["100"] is not state["games"]["100"]
    )


def test_actual_clock_is_resolved_from_current_facade(owner, monkeypatch):
    calls = []
    clock = SimpleNamespace(now=lambda zone: calls.append(zone) or NOW)
    monkeypatch.setattr(owner, "datetime", clock)
    assert owner._now(None) == NOW and calls == [timezone.utc]
    assert owner._now(NOW) == NOW and calls == [timezone.utc]
    with pytest.raises(ValueError, match="Clock must include a timezone"):
        owner._now(datetime(2026, 10, 1))


def test_http_uses_precise_deadline_timeout_and_four_per_second_wait(owner):
    client = Client([{"id": 20}])
    client.access_token = None
    ticks = iter([1.0, 1.1, 1.3, 1.4, 1.5, 1.6])
    result = owner._igdb(
        client, "external_games", "fields id;", 3.0, lambda: next(ticks)
    )
    assert result == [{"id": 20}]
    assert client.calls[:3] == [
        ("authenticate",),
        ("wait",),
        ("sleep", pytest.approx(0.15)),
    ]
    call = client.calls[3]
    assert call[0:2] == ("post", owner.IGDB_BASE + "external_games")
    assert call[2]["data"] == "fields id;"
    assert call[2]["headers"] == {
        "Client-ID": "fixture",
        "Authorization": "Bearer private-token",
    }
    timeout = call[2]["timeout"]
    assert isinstance(timeout, Timeout)
    assert timeout.total == pytest.approx(
        1.6
    ) and timeout.connect_timeout == pytest.approx(1.6)
    assert client._last_request_at == 1.5


def test_http_finally_stamps_failed_request_and_masks_secret_error(owner):
    client = Client(requests.ConnectionError("Bearer private-token"))
    with pytest.raises(requests.ConnectionError):
        owner._igdb(client, "games", "fields id;", None, lambda: 11.0)
    assert client._last_request_at == 11.0
    assert owner._error(client.response) == "ConnectionError"
    failure = requests.HTTPError(
        "private-token", response=SimpleNamespace(status_code=429)
    )
    assert owner._error(failure) == "HTTP 429"


@pytest.mark.parametrize("payload", [{}, None, [None], [1]])
def test_http_rejects_invalid_page_after_request(owner, payload):
    client = Client(payload)
    with pytest.raises(ValueError, match="Invalid IGDB mapping response"):
        owner._igdb(client, "games", "fields id;", None, lambda: 11.0)
    assert client._last_request_at == 11.0


def test_deadline_fails_before_authentication_and_is_checked_after_http(owner):
    client = Client([])
    client.access_token = None
    with pytest.raises(owner.CollectionDeadlineExceeded):
        owner._igdb(client, "games", "fields id;", 3.0, lambda: 3.0)
    assert client.calls == []
    client = Client([])
    client.request_interval = 0.25
    ticks = iter([1.0, 1.0, 1.0, 3.0, 3.0])
    with pytest.raises(owner.CollectionDeadlineExceeded):
        owner._igdb(client, "games", "fields id;", 3.0, lambda: next(ticks))
    assert client._last_request_at == 3.0 and any(
        row[0] == "post" for row in client.calls
    )


def test_pagination_uses_current_igdb_port_and_never_assumes_ten_rows_complete(
    owner, monkeypatch
):
    calls = []
    responses = iter([[{"id": value} for value in range(500)], [{"id": 500}]])
    monkeypatch.setattr(
        owner, "_igdb", lambda *args: calls.append(args) or next(responses)
    )
    rows = owner._pages("client", "games", "fields id;", 3.0, "clock")
    assert len(rows) == 501
    assert [row[2] for row in calls] == [
        "fields id; sort id asc; limit 500; offset 0;",
        "fields id; sort id asc; limit 500; offset 500;",
    ]
    assert all(
        row[0:2] == ("client", "games") and row[3:] == (3.0, "clock") for row in calls
    )


@pytest.mark.parametrize(
    "page_size,message",
    [(501, "page exceeded limit"), (500, "pagination exceeded safe limit")],
)
def test_pagination_rejects_oversize_and_unbounded_sources(
    owner, monkeypatch, page_size, message
):
    calls = []
    monkeypatch.setattr(
        owner, "_igdb", lambda *args: calls.append(args) or [{}] * page_size
    )
    with pytest.raises(ValueError, match=message):
        owner._pages(None, "games", "fields id;", None, lambda: 1)
    assert len(calls) == (1 if page_size == 501 else 100)


def test_refresh_current_source_ports_authoritative_ids_and_no_input_mutation(
    owner, monkeypatch
):
    calls = []

    def pages(client, endpoint, query, deadline, monotonic):
        calls.append((endpoint, query, deadline, monotonic()))
        if endpoint == "external_game_sources":
            return [{"id": 45, "name": "Steam"}]
        return [{"id": 1, "external_game_source": 45, "uid": "100", "game": 20}]

    monkeypatch.setattr(owner, "_pages", pages)
    client, source = Client(), catalog()
    source["source_commit"] = "a" * 40
    before = deepcopy(source)
    result = owner.refresh_mappings(
        client, source, now=NOW, deadline=5, monotonic=lambda: 1
    )
    row = result["games"]["100"]
    assert (
        row["status"] == "matched"
        and row["twitch_game_id"] == "300"
        and row["igdb_id"] == "20"
    )
    assert (
        row["steam"]["name"] == "Steam name"
        and row["twitch_name"] == "Different Twitch name"
    )
    assert client.calls == [("get", "games", [("igdb_id", "20")])]
    assert calls[1][1].endswith('external_game_source = 45 & uid = ("100");')
    assert source == before and result["source_catalog"]["commit"] == "a" * 40


def test_metadata_only_keeps_pending_without_ever_calling_network(owner, monkeypatch):
    monkeypatch.setattr(
        owner, "_pages", lambda *args: pytest.fail("metadata-only lookup")
    )
    result = owner.refresh_mappings(
        SimpleNamespace(), catalog(), now=NOW, allow_lookup=False
    )
    assert result["games"]["100"]["status"] == "pending"
    assert (
        result["report"]["lookup_count"] == 0
        and result["report"]["pending_lookup_count"] == 1
    )


@pytest.mark.parametrize(
    "failure",
    [
        requests.ConnectionError("private-token"),
        ValueError("private-token"),
        KeyError("private-token"),
        TypeError("private-token"),
        RuntimeError("private-token"),
        OverflowError("private-token"),
        OSError("private-token"),
    ],
)
def test_refresh_handles_same_source_failure_scope_without_secret_text(
    owner, monkeypatch, failure
):
    def failing(*args):
        raise failure

    monkeypatch.setattr(owner, "_pages", failing)
    result = owner.refresh_mappings(SimpleNamespace(), catalog(), now=NOW)
    assert result["games"]["100"]["status"] == "pending"
    assert result["report"]["errors"] == [
        {"stage": "external_game_sources", "reason": type(failure).__name__}
    ]


def test_refresh_preserves_non_handled_source_exception(owner, monkeypatch):
    def failing(*args):
        raise IndexError("not a lookup failure")

    monkeypatch.setattr(owner, "_pages", failing)
    with pytest.raises(IndexError, match="not a lookup failure"):
        owner.refresh_mappings(SimpleNamespace(), catalog(), now=NOW)


def test_helix_partial_batch_keeps_previous_decision_intact(owner, monkeypatch):
    game = owner.normalize_steam_catalog(catalog(), NOW)[0]
    mapping = {
        "schema_version": 1,
        "steam_source_id": "45",
        "games": {
            "100": {
                "steam_appid": "100",
                "status": "unmatched",
                "steam": game,
                "checked_at": "2026-09-29T08:00:00Z",
                "retry_at": "2026-09-30T08:00:00Z",
                "reason": "no_igdb_steam_link",
            }
        },
    }
    before = deepcopy(mapping)
    monkeypatch.setattr(
        owner,
        "_pages",
        lambda *args: [{"external_game_source": 45, "uid": "100", "game": 20}],
    )
    client = SimpleNamespace(get=lambda *args, **kwargs: {"data": [None]})
    result = owner.refresh_mappings(client, catalog(), mapping, NOW)
    row = result["games"]["100"]
    assert (
        row["status"] == "unmatched"
        and row["checked_at"] == before["games"]["100"]["checked_at"]
    )
    assert (
        row["reason"] == "no_igdb_steam_link"
        and "igdb_id" not in row
        and mapping == before
    )
    assert result["report"]["errors"] == [
        {"stage": "mapping_batch", "reason": "ValueError"}
    ]
