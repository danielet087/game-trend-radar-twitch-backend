"""Canonical discovery must preserve source evidence, clocks and helper seams."""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
import importlib
import inspect
from pathlib import Path
import subprocess
import sys

import pytest
import requests

from collectors import twitch_steam_discovery as legacy
from radar_backend.adapters import twitch_steam_discovery as canonical
from radar_backend.domain import twitch_steam_discovery as domain
from tests.test_twitch_steam_discovery import (
    AT,
    FIRST,
    NOW,
    Client,
    catalog,
    category,
    external,
    first_responses,
    tracking,
)
from tests.test_twitch_missing_helix_igdb_fallback import (
    APPID,
    IGDB_ID,
    TWITCH_ID,
    Client as FallbackClient,
    catalog as fallback_catalog,
    tracking as fallback_tracking,
    NOW as FALLBACK_NOW,
)
from tests.test_twitch_steam_website_discovery import website_identity

OWNERS = (legacy, canonical)


def website_owner(owner):
    prefix = "collectors" if owner is legacy else "radar_backend.adapters"
    return importlib.import_module(prefix + ".twitch_steam_website_identity")


@pytest.mark.parametrize("owner", OWNERS, ids=("legacy", "canonical"))
@pytest.mark.parametrize(
    "name,parameters",
    [
        ("_ids", ("value",)),
        ("_enrollment", ("value", "clock")),
        ("_twitch_identity", ("value", "twitch_id", "igdb_id")),
        ("_invalidate_identity", ("row", "reason", "at", "retry_at")),
        ("normalize_discovery_state", ("payload",)),
        ("_catalog_ids", ("payload",)),
        ("_qualified_members", ("tracking", "clock")),
        (
            "refresh_discoveries",
            (
                "client",
                "tracking_state",
                "public_catalog",
                "discovery_state",
                "now",
                "deadline",
                "monotonic",
            ),
        ),
    ],
)
def test_original_callable_contract_and_bound_defaults(owner, name, parameters):
    function = getattr(owner, name)
    signature = inspect.signature(function)
    assert tuple(signature.parameters) == parameters
    if name == "refresh_discoveries":
        assert signature.parameters["discovery_state"].default is None
        assert signature.parameters["now"].default is None
        assert signature.parameters["deadline"].kind is inspect.Parameter.KEYWORD_ONLY
        assert signature.parameters["deadline"].default is None
        assert signature.parameters["monotonic"].default is owner.time.monotonic
    elif name == "normalize_discovery_state":
        assert signature.parameters["payload"].default is None
    elif name == "_enrollment":
        assert signature.parameters["clock"].default is None


@pytest.mark.parametrize("owner", OWNERS, ids=("legacy", "canonical"))
def test_direct_lookup_keeps_only_exact_official_links_and_does_not_mutate_inputs(
    owner,
):
    source = tracking()
    public = catalog(100)
    before = deepcopy((source, public))
    client = Client(
        first_responses(
            external(101, external_id=2),
            external(),
            external(999, source=1),
            external(999, igdb_id=21),
        )
    )
    state = owner.refresh_discoveries(client, source, public, now=NOW)
    row = state["games"]["300"]
    assert (source, public) == before
    assert row["steam_appids"] == ["100", "101"]
    assert row["public_steam_appids"] == ["100"]
    assert row["missing_public_appids"] == ["101"]
    assert [link["external_game_id"] for link in row["links"]] == ["1", "2"]
    assert row["first_seen_at"] == FIRST and row["checked_at"] == AT
    assert client.calls[0] == ("games", [("id", "300")])
    assert [endpoint for endpoint, _ in client.calls] == [
        "games",
        "external_game_sources",
        "external_games",
    ]


@pytest.mark.parametrize("owner", OWNERS, ids=("legacy", "canonical"))
def test_blank_helix_igdb_id_uses_exact_twitch_uid_with_complete_proof(owner):
    client = FallbackClient()
    state = owner.refresh_discoveries(
        client, fallback_tracking(), fallback_catalog(), now=FALLBACK_NOW
    )
    row = state["games"][TWITCH_ID]
    assert row["igdb_id"] == IGDB_ID and row["steam_appids"] == [APPID]
    assert row["igdb_identity"]["links"][0]["uid"] == TWITCH_ID
    assert row["lookup_policy_version"] == 2
    assert [endpoint for endpoint, _ in client.calls] == [
        "helix_games",
        "external_game_sources",
        "external_games",
        "external_game_sources",
        "external_games",
    ]


@pytest.mark.parametrize("owner", OWNERS, ids=("legacy", "canonical"))
def test_partial_external_page_keeps_completed_decision_and_does_not_forge_time(owner):
    previous = owner.refresh_discoveries(
        Client(first_responses(external())), tracking(), catalog(), now=NOW
    )
    before = deepcopy(previous)
    client = Client(
        [
            ("games", {"data": [category()]}),
            ("external_games", [external(999, external_id=i + 1) for i in range(500)]),
            ("external_games", requests.ConnectionError("Authorization SECRET")),
        ]
    )
    updated = owner.refresh_discoveries(
        client, tracking(), catalog(100), previous, NOW + timedelta(days=1)
    )
    assert previous == before
    row = updated["games"]["300"]
    assert row["steam_appids"] == ["100"] and row["checked_at"] == AT
    assert row["public_steam_appids"] == ["100"]
    assert "SECRET" not in repr(updated)
    assert "offset 500;" in client.calls[-1][1]


@pytest.mark.parametrize("owner", OWNERS, ids=("legacy", "canonical"))
@pytest.mark.parametrize(
    "elapsed,requests_due",
    [
        (timedelta(hours=24) - timedelta(microseconds=1), False),
        (timedelta(hours=24), True),
        (timedelta(hours=24, microseconds=1), True),
    ],
)
def test_completed_identity_cache_has_original_twenty_four_hour_boundary(
    owner, elapsed, requests_due
):
    previous = owner.refresh_discoveries(
        Client(first_responses(external())), tracking(), catalog(), now=NOW
    )
    responses = (
        [("games", {"data": [category()]}), ("external_games", [external()])]
        if requests_due
        else []
    )
    client = Client(responses)
    result = owner.refresh_discoveries(
        client, tracking(), catalog(), previous, NOW + elapsed
    )
    assert bool(client.calls) is requests_due
    assert result["report"]["lookup_count"] == int(requests_due)
    assert result["games"]["300"]["checked_at"] == (
        owner.timestamp(NOW + elapsed) if requests_due else AT
    )


@pytest.mark.parametrize("owner", OWNERS, ids=("legacy", "canonical"))
def test_website_runtime_owner_is_used_and_outage_retries_without_direct_lookup(
    owner, monkeypatch
):
    lookups = []

    def failure(client, twitch_id, igdb_id, now, *, deadline, monotonic):
        lookups.append((twitch_id, igdb_id, now, deadline, monotonic()))
        raise requests.ConnectionError("SECRET bearer token")

    monkeypatch.setattr(website_owner(owner), "lookup_website_identity", failure)
    previous = owner.refresh_discoveries(
        Client(first_responses()),
        tracking(),
        catalog(),
        now=NOW,
        deadline=20,
        monotonic=lambda: 0,
    )
    row = previous["games"]["300"]
    assert lookups == [("300", "20", NOW, 20, 0)]
    assert row["checked_at"] == AT and row["related_retry_at"] is None
    assert row["related_lookup_status"] == "unavailable"
    assert "SECRET" not in repr(previous)
    proof = website_identity(at="2026-10-02T10:00:00Z")
    monkeypatch.setattr(
        website_owner(owner), "lookup_website_identity", lambda *args, **kwargs: proof
    )
    client = Client([])
    result = owner.refresh_discoveries(
        client, tracking(), catalog(), previous, NOW + timedelta(hours=1)
    )
    assert client.calls == [] and result["report"]["lookup_count"] == 0
    assert result["report"]["related_lookup_count"] == 1
    assert result["games"]["300"]["related_steam_identity"] == proof
    assert result["games"]["300"]["checked_at"] == AT


@pytest.mark.parametrize("owner", OWNERS, ids=("legacy", "canonical"))
def test_contradictory_new_owner_removes_independent_proof_before_failed_lookup(
    owner, monkeypatch
):
    monkeypatch.setattr(
        website_owner(owner),
        "lookup_website_identity",
        lambda *args, **kwargs: website_identity(),
    )
    previous = owner.refresh_discoveries(
        Client(first_responses()), tracking(), catalog(), now=NOW
    )
    monkeypatch.setattr(
        website_owner(owner),
        "lookup_website_identity",
        lambda *args, **kwargs: pytest.fail("invalid old owner"),
    )
    client = Client(
        [
            ("games", {"data": [category(igdb_id=21)]}),
            ("external_games", requests.ConnectionError("outage")),
        ]
    )
    updated = owner.refresh_discoveries(
        client, tracking(), catalog(), previous, NOW + timedelta(days=1)
    )
    row = updated["games"]["300"]
    assert row["igdb_id"] is None and row["status"] == "unavailable"
    assert row["steam_appids"] == row["links"] == []
    assert row["checked_at"] == "2026-10-03T09:00:00Z"
    assert all(
        key not in row
        for key in ("igdb_identity", "related_steam_identity", "related_lookup_status")
    )


@pytest.mark.parametrize("owner", OWNERS, ids=("legacy", "canonical"))
def test_current_clock_parser_rules_and_request_ports_are_injected(owner, monkeypatch):
    trace = []
    original_now = owner._now
    original_parse = owner.parse_timestamp
    original_catalog = owner._catalog_ids
    original_tracking = owner.normalize_tracking_state
    original_qualified = owner._qualified_members
    original_normalize = owner.normalize_discovery_state

    def observe(name, function):
        def wrapped(*args, **kwargs):
            trace.append(name)
            return function(*args, **kwargs)

        return wrapped

    for name, function in (
        ("_now", original_now),
        ("_catalog_ids", original_catalog),
        ("normalize_tracking_state", original_tracking),
        ("_qualified_members", original_qualified),
        ("normalize_discovery_state", original_normalize),
    ):
        monkeypatch.setattr(owner, name, observe(name, function))
    monkeypatch.setattr(owner, "parse_timestamp", observe("parse", original_parse))
    original_enrollment = owner._enrollment
    monkeypatch.setattr(
        owner, "_enrollment", observe("enrollment", original_enrollment)
    )
    monkeypatch.setattr(owner, "_pages", lambda *args, **kwargs: pytest.fail("not due"))
    monkeypatch.setattr(
        owner,
        "_website_lookup",
        lambda: (trace.append("website_import") or (lambda *args, **kwargs: None)),
    )
    result = owner.refresh_discoveries(
        Client([("games", {"data": []})]), tracking(), catalog(), now=NOW
    )
    assert trace[0:2] == ["_now", "_catalog_ids"]
    assert (
        trace.index("normalize_tracking_state")
        < trace.index("_qualified_members")
        < trace.index("normalize_discovery_state")
    )
    assert "enrollment" in trace and "parse" in trace
    assert trace.count("enrollment") == 2
    assert trace.count("_now") == 1
    assert trace.index("website_import") < len(trace) - 1 - trace[::-1].index(
        "normalize_discovery_state"
    )
    assert trace.count("normalize_discovery_state") == 2
    assert trace.count("website_import") == 1
    assert result["games"]["300"]["status"] == "pending"


@pytest.mark.parametrize("owner", OWNERS, ids=("legacy", "canonical"))
def test_custom_failure_tuple_and_error_formatter_use_current_facade_globals(
    owner, monkeypatch
):
    class CustomFailure(Exception):
        pass

    monkeypatch.setattr(owner, "FAILURES", (CustomFailure,))
    monkeypatch.setattr(owner, "_error", lambda error: "opaque custom failure")
    client = Client([("games", CustomFailure("SECRET"))])
    result = owner.refresh_discoveries(client, tracking(), catalog(), now=NOW)
    assert result["games"]["300"]["status"] == "unavailable"
    assert result["report"]["errors"] == [
        {
            "stage": "helix_games",
            "twitch_game_ids": ["300"],
            "reason": "opaque custom failure",
        }
    ]


@pytest.mark.parametrize("owner", OWNERS, ids=("legacy", "canonical"))
@pytest.mark.parametrize("helper", ("_id", "parse_timestamp", "deepcopy"))
def test_explicit_none_helper_is_not_replaced_by_a_domain_default(
    owner, helper, monkeypatch
):
    monkeypatch.setattr(owner, helper, None)
    with pytest.raises(TypeError):
        if helper == "_id":
            owner._ids([100])
        else:
            owner._enrollment(tracking()["games"]["300"]["enrollment"])


@pytest.mark.parametrize(
    "owner", (domain, legacy, canonical), ids=("domain", "legacy", "canonical")
)
def test_domain_proof_validation_is_pure_and_copies_full_nested_evidence(owner):
    state = canonical.refresh_discoveries(
        Client(first_responses(external())), tracking(), catalog(), now=NOW
    )
    before = deepcopy(state)
    normalized = owner.normalize_discovery_state(state)
    normalized["games"]["300"]["links"][0]["uid"] = "999"
    normalized["games"]["300"]["twitch_enrollment"]["viewer_count"] = 1
    normalized["source_catalog"]["appids"].append("999")
    assert state == before
    assert owner.normalize_discovery_state(None) == {
        "schema_version": 1,
        "updated_at": None,
        "games": {},
    }


@pytest.mark.parametrize(
    "owner", (domain, legacy, canonical), ids=("domain", "legacy", "canonical")
)
@pytest.mark.parametrize(
    "changes",
    [
        {"steam_appids": ["100", "100"]},
        {"method": "name_similarity"},
        {"checked_at": None},
        {"lookup_policy_version": True},
        {"public_steam_appids": ["100"], "missing_public_appids": ["100"]},
    ],
)
def test_all_owners_reject_inconsistent_persisted_identity_proof(owner, changes):
    state = canonical.refresh_discoveries(
        Client(first_responses(external())), tracking(), catalog(), now=NOW
    )
    state["games"]["300"].update(changes)
    before = deepcopy(state)
    with pytest.raises(ValueError):
        owner.normalize_discovery_state(state)
    assert state == before


@pytest.mark.parametrize("owner", OWNERS, ids=("legacy", "canonical"))
def test_deadline_port_checks_before_and_after_helix_and_prevents_followup(
    owner, monkeypatch
):
    trace = []
    original_deadline = owner._deadline

    def check(deadline, monotonic):
        trace.append("deadline")
        return original_deadline(deadline, monotonic)

    monkeypatch.setattr(owner, "_deadline", check)
    clock = iter((0, 10))
    client = Client([("games", {"data": [category()]})])
    result = owner.refresh_discoveries(
        client,
        tracking(),
        catalog(),
        now=NOW,
        deadline=10,
        monotonic=lambda: next(clock),
    )
    assert trace == ["deadline", "deadline"]
    assert len(client.calls) == 1
    assert result["report"]["deadline_exhausted"] is True
    assert result["games"]["300"]["checked_at"] is None


def test_pure_domain_and_application_do_not_import_side_effect_owners():
    program = """
import builtins
original = builtins.__import__
def guard(name, *args, **kwargs):
    if name == 'requests' or name.startswith(('collectors', 'scripts', 'radar_backend.adapters', 'radar_backend.state', 'radar_backend.publication', 'radar_backend.jobs')):
        raise AssertionError(name)
    return original(name, *args, **kwargs)
builtins.__import__ = guard
from radar_backend.domain import twitch_steam_discovery as domain
from radar_backend.application import twitch_steam_discovery as application
assert domain._ids(['002', 1]) == ['1', '2']
assert domain.normalize_discovery_state() == {'schema_version': 1, 'updated_at': None, 'games': {}}
assert callable(application.refresh_discoveries)
"""
    subprocess.run(
        [sys.executable, "-c", program],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    )


def test_canonical_adapter_executes_without_any_legacy_owner():
    program = """
import builtins
from datetime import datetime, timezone
original = builtins.__import__
def guard(name, *args, **kwargs):
    if name == 'collectors' or name == 'scripts' or name.startswith(('collectors.', 'scripts.')):
        raise AssertionError(name)
    return original(name, *args, **kwargs)
builtins.__import__ = guard
from radar_backend.adapters import twitch_steam_discovery as discovery
class Client:
    def get(self, endpoint, *, params):
        raise AssertionError('unqualified tracking cannot look up any identity')
clock = datetime(2026, 10, 2, 9, tzinfo=timezone.utc)
state = discovery.refresh_discoveries(Client(), {'schema_version': 1, 'updated_at': None, 'games': {}},
    {'version': 2, 'generated_at': '2026-10-02T09:00:00Z', 'count': 0, 'games': []}, now=clock)
assert state['games'] == {}
assert state['report']['lookup_count'] == state['report']['related_lookup_count'] == 0
"""
    subprocess.run(
        [sys.executable, "-c", program],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    )
