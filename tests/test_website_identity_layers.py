"""The website chain runs through explicit ports and both public compositions."""

from copy import deepcopy
import inspect
from pathlib import Path
import subprocess
import sys
import types

import pytest
import requests

from collectors import twitch_steam_website_identity as legacy
from radar_backend.adapters import twitch_steam_website_identity as canonical
from radar_backend.application import twitch_steam_website_identity as application
from radar_backend.domain import twitch_steam_website_identity as rules
from radar_backend.domain.twitch import CollectionDeadlineExceeded
from radar_backend.domain.twitch_newness import timestamp
from tests.test_twitch_steam_website_identity import (
    APPID,
    CHECKED,
    IGDB_ID,
    NOW,
    STORE_URL,
    TWITCH_ID,
    Response,
    proof,
    steam_payload,
)


@pytest.fixture(params=[legacy, canonical], ids=["legacy", "canonical"])
def facade(request):
    return request.param


def compose_offline(monkeypatch, facade, *, source=STORE_URL, payload=None):
    trace = []

    def pages(client, endpoint, query, deadline, monotonic):
        trace.append((endpoint, query))
        return (
            [{"id": int(IGDB_ID), "websites": [456]}]
            if endpoint == "games"
            else [{"id": 456, "game": int(IGDB_ID), "url": source}]
        )

    def request_get(url, **kwargs):
        timeout = kwargs.pop("timeout")
        trace.append(
            (
                "steam",
                url,
                kwargs,
                timeout.total,
                timeout.connect_timeout,
                timeout.read_timeout,
            )
        )
        return Response(payload)

    monkeypatch.setattr(facade, "_pages", pages)
    # Replacing the module global proves no request handle was captured at import.
    monkeypatch.setattr(facade, "requests", types.SimpleNamespace(get=request_get))
    return trace


def lookup(facade, **kwargs):
    return facade.lookup_website_identity(object(), TWITCH_ID, IGDB_ID, NOW, **kwargs)


def test_both_compositions_preserve_exact_keyless_store_contract(monkeypatch, facade):
    trace = compose_offline(monkeypatch, facade)
    linked = lookup(facade, deadline=15.0, monotonic=lambda: 10.0)
    assert linked == proof()
    assert trace == [
        ("games", f"fields id,websites; where id = {IGDB_ID};"),
        ("websites", "fields id,url,game; where id = (456);"),
        (
            "steam",
            canonical.APPDETAILS_URL,
            {
                "params": {"appids": APPID, "cc": "tw", "l": "tchinese"},
                "allow_redirects": False,
            },
            5.0,
            5.0,
            5.0,
        ),
    ]


@pytest.mark.parametrize(
    "source",
    [
        "http://store.steampowered.com/app/3070070/",
        "https://store.steampowered.com:443/app/3070070/",
        "https://store.steampowered.com.evil.test/app/3070070/",
        STORE_URL + "?",
        STORE_URL + "#",
        STORE_URL + "../other/",
    ],
)
def test_unsafe_evidence_rejected_before_transport(monkeypatch, facade, source):
    trace = compose_offline(monkeypatch, facade, source=source)
    with pytest.raises(ValueError):
        lookup(facade)
    assert [row[0] for row in trace] == ["games", "websites"]


@pytest.mark.parametrize("ids", [[3], [4], [True], ["1"], [-1]])
def test_official_exclusion_descriptors_cannot_label_category(monkeypatch, facade, ids):
    payload = steam_payload()
    payload[APPID]["data"]["content_descriptors"]["ids"] = ids
    compose_offline(monkeypatch, facade, payload=payload)
    with pytest.raises(ValueError):
        lookup(facade)


def test_runtime_helpers_and_timeout_are_resolved_by_the_facade(monkeypatch, facade):
    trace = compose_offline(monkeypatch, facade)
    original_store_day = facade._store_day
    original_timeout = facade.Timeout
    dates, timeouts = [], []

    def store_day(value):
        dates.append(value)
        return original_store_day(value)

    def timeout(**kwargs):
        timeouts.append(kwargs)
        return original_timeout(**kwargs)

    monkeypatch.setattr(facade, "_store_day", store_day)
    monkeypatch.setattr(facade, "Timeout", timeout)
    assert (
        lookup(facade)["steam_identity_metadata"]["release_store_date"] == "2015-07-06"
    )
    assert dates == ["6 Jul, 2015", "6 Jul, 2015"]
    assert timeouts == [{"total": 20.0, "connect": 10.0, "read": 20.0}]
    assert trace[-1][0] == "steam"


def test_deadline_after_response_json_rejects_the_complete_payload(monkeypatch, facade):
    trace = compose_offline(monkeypatch, facade)
    clock = {"value": 1.0}

    class SlowJSON(Response):
        def json(self):
            clock["value"] = 10.0
            return super().json()

    monkeypatch.setattr(
        facade,
        "requests",
        types.SimpleNamespace(get=lambda *args, **kwargs: SlowJSON()),
    )
    with pytest.raises(CollectionDeadlineExceeded):
        lookup(facade, deadline=10.0, monotonic=lambda: clock["value"])
    assert [row[0] for row in trace] == ["games", "websites"]


def test_http_raise_precedes_status_validation_and_payload_read(monkeypatch, facade):
    events = []

    class FailedResponse:
        status_code = 500

        def raise_for_status(self):
            events.append("raise")
            raise requests.HTTPError("official failure")

        def json(self):
            pytest.fail("a failed response cannot be decoded")

    monkeypatch.setattr(facade, "_deadline", lambda *args: events.append("deadline"))
    monkeypatch.setattr(
        facade,
        "requests",
        types.SimpleNamespace(get=lambda *args, **kwargs: FailedResponse()),
    )
    with pytest.raises(requests.HTTPError, match="official failure"):
        facade._steam_metadata(
            APPID, CHECKED, deadline=None, monotonic=lambda: pytest.fail("unused clock")
        )
    assert events == ["deadline", "deadline", "raise"]


def test_lookup_keeps_final_normalizer_and_store_callbacks_current(monkeypatch, facade):
    compose_offline(monkeypatch, facade)
    expected = object()
    seen = []

    def store(appid, checked_at, **kwargs):
        seen.append((appid, checked_at, kwargs["deadline"]))
        # The original normalizer is looked up only after the store callback returns.
        monkeypatch.setattr(
            facade, "normalize_website_identity", lambda *args: expected
        )
        return deepcopy(proof()["steam_identity_metadata"])

    monkeypatch.setattr(facade, "_steam_metadata", store)
    assert lookup(facade, deadline=15.0, monotonic=lambda: 10.0) is expected
    assert seen == [(APPID, CHECKED, 15.0)]


def test_application_uses_injected_ports_in_the_original_call_order():
    events = []
    source = proof()

    def pages(client, endpoint, query, deadline, monotonic):
        events.append(endpoint)
        return (
            [{"id": int(IGDB_ID), "websites": [456]}]
            if endpoint == "games"
            else [{"id": 456, "game": int(IGDB_ID), "url": STORE_URL}]
        )

    def store(appid, checked_at, **kwargs):
        events.append("store")
        return deepcopy(source["steam_identity_metadata"])

    def normalize(value, twitch_id, igdb_id):
        events.append("normalize")
        return rules.normalize_website_identity(value, twitch_id, igdb_id)

    result = application.lookup_website_identity(
        object(),
        TWITCH_ID,
        IGDB_ID,
        NOW,
        deadline=None,
        monotonic=lambda: 0.0,
        _canonical_id=rules._canonical_id,
        timestamp=timestamp,
        _now=lambda value: events.append("clock") or value,
        _deadline=lambda *args: events.append("deadline"),
        _pages=pages,
        _store_url=rules._store_url,
        _steam_metadata=store,
        normalize_website_identity=normalize,
        method=lambda: rules.METHOD,
    )
    assert result == source
    assert result is not source
    assert events == [
        "clock",
        "deadline",
        "games",
        "websites",
        "store",
        "deadline",
        "normalize",
    ]


def test_pure_proof_validation_owns_independent_nested_objects():
    source = proof()
    result = rules.normalize_website_identity(source, TWITCH_ID, IGDB_ID)
    result["website_links"][0]["website_id"] = "789"
    result["steam_identity_metadata"]["content_descriptor_ids"].append(0)
    result["steam_identity_metadata"]["raw_release_date"]["date"] = "other"
    assert source == proof()


def test_domain_and_application_import_without_old_owners_or_io_layers():
    code = """
import builtins, copy, datetime, importlib, json, re, typing, urllib.parse, zoneinfo
original = builtins.__import__
blocked = ("collectors", "scripts", "requests", "urllib3", "radar_backend.adapters", "radar_backend.state")
def guarded(name, *args, **kwargs):
    if any(name == owner or name.startswith(owner + ".") for owner in blocked):
        raise AssertionError("forbidden layer: " + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
rules = importlib.import_module("radar_backend.domain.twitch_steam_website_identity")
application = importlib.import_module("radar_backend.application.twitch_steam_website_identity")
assert rules._store_url("https://store.steampowered.com/app/3070070/Name/") == ("3070070", "https://store.steampowered.com/app/3070070/")
assert rules._store_day("2015 年 7 月 6 日") == "2015-07-06"
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_facade_private_and_public_signatures_and_bound_clock_match():
    expected = {
        "_canonical_id": ("value",),
        "_proof_id": ("value",),
        "_store_url": ("value",),
        "_store_day": ("raw",),
        "_descriptor_ids": ("value",),
        "normalize_website_identity": ("value", "twitch_id", "igdb_id"),
        "_steam_metadata": ("appid", "checked_at", "deadline", "monotonic"),
        "lookup_website_identity": (
            "client",
            "twitch_id",
            "igdb_id",
            "now",
            "deadline",
            "monotonic",
        ),
    }
    for name, names in expected.items():
        left = inspect.signature(getattr(legacy, name))
        right = inspect.signature(getattr(canonical, name))
        assert left == right
        assert tuple(left.parameters) == names
    signature = inspect.signature(canonical.lookup_website_identity)
    assert signature.parameters["deadline"].kind == inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["deadline"].default is None
    assert signature.parameters["monotonic"].default is legacy.time.monotonic
