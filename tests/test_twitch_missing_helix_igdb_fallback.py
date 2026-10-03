"""A missing Helix IGDB ID requires another official identity chain."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import pytest
import requests

from collectors.twitch_steam_discovery import METHOD, POLICY_VERSION, refresh_discoveries


NOW = datetime(2026, 10, 3, 14, tzinfo=timezone.utc)
AT = "2026-10-03T14:00:00Z"
FIRST = "2026-10-02T09:00:00Z"
TWITCH_ID = "1288749557"
IGDB_ID = "365465"
APPID = "4019220"
TWITCH_SOURCE = "77"
STEAM_SOURCE = "45"


def enrollment():
    return {"source": "twitch_directory_dom", "viewer_count": 9000,
            "min_viewers": 7000, "observed_at": FIRST}


def tracking(candidate=IGDB_ID):
    source = {"source": "twitch_new", "status": "active", "first_seen_at": FIRST,
              "release_at": None, "expires_at": None, "enrollment": enrollment()}
    entry = {"game_id": TWITCH_ID, "game_name": "Dressmaker", "status": "active",
             "first_seen_at": FIRST, "updated_at": FIRST, "enrollment": enrollment(),
             "tracking_sources": {"twitch_new": source}}
    if candidate is not None:
        entry["igdb_id"] = candidate
    return {"schema_version": 1, "updated_at": FIRST, "games": {TWITCH_ID: entry}}


def catalog():
    return {"version": 2, "count": 0, "generated_at": FIRST, "games": []}


def category(igdb_id=""):
    return {"id": TWITCH_ID, "igdb_id": igdb_id, "name": "A completely different official name"}


def twitch_link(**changes):
    return {"id": 500, "uid": TWITCH_ID, "game": int(IGDB_ID),
            "external_game_source": int(TWITCH_SOURCE), **changes}


def steam_link(**changes):
    return {"id": 600, "uid": APPID, "game": int(IGDB_ID),
            "external_game_source": int(STEAM_SOURCE), **changes}


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        if isinstance(self.payload, Exception):
            raise self.payload

    def json(self):
        return self.payload


class Client:
    """Route official ID queries without depending on harmless cache ordering."""
    def __init__(self, *, helix=None, twitch_pages=None, steam_pages=None, sources=None):
        self.helix = {"data": [category()]} if helix is None else helix
        self.twitch_pages = [[twitch_link()]] if twitch_pages is None else list(twitch_pages)
        self.steam_pages = [[steam_link()]] if steam_pages is None else list(steam_pages)
        self.sources = ([{"id": int(STEAM_SOURCE), "name": "Steam"},
                         {"id": int(TWITCH_SOURCE), "name": "tWiTcH"}]
                        if sources is None else sources)
        self.calls = []
        self.session = self
        self.client_id, self.access_token, self.timeout_seconds = "client", "token", 20

    def _wait(self):
        pass

    def authenticate(self):
        self.access_token = "token"

    def get(self, endpoint, *, params):
        assert endpoint == "games"
        self.calls.append(("helix_games", params))
        if isinstance(self.helix, Exception):
            raise self.helix
        return deepcopy(self.helix)

    def post(self, url, **kwargs):
        endpoint, query = url.rsplit("/", 1)[1], kwargs["data"]
        self.calls.append((endpoint, query))
        if endpoint == "external_game_sources":
            return Response(deepcopy(self.sources))
        if endpoint == "games":
            assert "fields id,websites;" in query and f"where id = {IGDB_ID};" in query
            return Response([{"id": int(IGDB_ID), "websites": []}])
        assert endpoint == "external_games", "Fallback cannot use name search or invented IGDB fields"
        if f"external_game_source = {TWITCH_SOURCE}" in query:
            assert f'"{TWITCH_ID}"' in query, "Twitch link lookup must use the exact UID"
            assert "uid" in query and "search" not in query
            return Response(self.twitch_pages.pop(0))
        assert f"external_game_source = {STEAM_SOURCE}" in query
        return Response(self.steam_pages.pop(0))


def run(client, *, registry=None, state=None, now=NOW):
    return refresh_discoveries(client, tracking() if registry is None else registry,
                               catalog(), state, now=now)


def legacy_state(status="pending"):
    row = {
        "twitch_game_id": TWITCH_ID, "twitch_name": "Dressmaker", "igdb_id": None,
        "status": status, "method": METHOD, "steam_appids": [], "links": [],
        "first_seen_at": FIRST, "checked_at": None,
        "retry_at": "2026-10-04T09:00:00Z", "twitch_enrollment": enrollment(),
        "active": True, "updated_at": FIRST, "public_steam_appids": [], "missing_public_appids": [],
        "reason": "missing_twitch_igdb_identity",
    }
    if status == "no_steam_link":
        row.update(igdb_id=IGDB_ID, checked_at=FIRST, reason="no_igdb_steam_link")
    return {"schema_version": 1, "updated_at": FIRST, "steam_source_id": STEAM_SOURCE,
            "games": {TWITCH_ID: row}}


@pytest.mark.parametrize("candidate", [IGDB_ID, None])
def test_exact_official_twitch_uid_recovers_dressmaker_without_name_matching(candidate):
    registry = tracking(candidate)
    before = deepcopy(registry)
    client = Client()

    state = run(client, registry=registry)

    row = state["games"][TWITCH_ID]
    assert registry == before
    assert row["status"] == "matched" and row["igdb_id"] == IGDB_ID
    assert row["steam_appids"] == [APPID] and row["checked_at"] == AT
    assert row["retry_at"] == "2026-10-04T14:00:00Z"
    assert row["lookup_policy_version"] == POLICY_VERSION == 2
    assert state["twitch_source_id"] == TWITCH_SOURCE
    assert row["igdb_identity"] == {
        "method": "igdb_external_twitch_uid_v1", "twitch_game_id": TWITCH_ID,
        "igdb_id": IGDB_ID, "twitch_source_id": TWITCH_SOURCE, "checked_at": AT,
        "links": [{"external_game_id": "500", "external_game_source": TWITCH_SOURCE,
                   "uid": TWITCH_ID, "game": IGDB_ID}],
    }
    assert row["links"][0]["url"] == f"https://store.steampowered.com/app/{APPID}/"
    assert row["twitch_name"] == "A completely different official name"
    assert len([query for endpoint, query in client.calls
                if endpoint == "external_games" and f"external_game_source = {TWITCH_SOURCE}" in query]) == 1


@pytest.mark.parametrize("links,checked_at", [
    ([], None),
    ([twitch_link(game=999)], AT),
    ([twitch_link(uid="999")], None),
    ([twitch_link(external_game_source=1)], None),
    ([twitch_link(id="not-an-id")], None),
    ([twitch_link(game=None)], None),
    ([twitch_link(), twitch_link(id=501, game=999)], AT),
    ([twitch_link(), twitch_link(id=501, uid="999")], None),
    ({"id": 500, "game": int(IGDB_ID)}, None),
])
def test_missing_conflicting_or_malformed_official_twitch_links_do_not_assert_steam(links, checked_at):
    client = Client(twitch_pages=[links], steam_pages=[])

    state = run(client)

    row = state["games"][TWITCH_ID]
    assert row["status"] != "matched" and row["steam_appids"] == []
    assert row["checked_at"] == checked_at
    assert not any(endpoint == "external_games" and f"external_game_source = {STEAM_SOURCE}" in query
                   for endpoint, query in client.calls)


@pytest.mark.parametrize("sources", [
    [{"id": int(STEAM_SOURCE), "name": "Steam"}],
    [{"id": int(STEAM_SOURCE), "name": "Steam"}, {"id": 77, "name": "Twitch"},
     {"id": 78, "name": "Twitch"}],
    [{"id": int(STEAM_SOURCE), "name": "Steam"}, {"id": "invalid", "name": "Twitch"}],
])
def test_twitch_source_must_be_identified_uniquely_without_assumed_source_number(sources):
    state = run(Client(sources=sources, twitch_pages=[], steam_pages=[]))

    assert state["games"][TWITCH_ID]["status"] != "matched"
    assert state["games"][TWITCH_ID]["checked_at"] is None


def test_absent_fresh_helix_category_never_uses_tracking_candidate():
    client = Client(helix={"data": []}, twitch_pages=[], steam_pages=[])

    row = run(client)["games"][TWITCH_ID]

    assert row["status"] == "pending" and row["checked_at"] is None
    assert row["steam_appids"] == []
    assert len(client.calls) == 1


def test_fresh_nonempty_helix_identity_takes_precedence_over_old_tracking_candidate():
    official_igdb = "400000"
    client = Client(helix={"data": [category(official_igdb)]}, twitch_pages=[],
                    steam_pages=[[steam_link(game=int(official_igdb))]])

    row = run(client)["games"][TWITCH_ID]

    assert row["status"] == "matched" and row["igdb_id"] == official_igdb
    assert row["steam_appids"] == [APPID]
    assert not any(endpoint == "external_games" and f"external_game_source = {TWITCH_SOURCE}" in query
                   for endpoint, query in client.calls)


def test_successful_fallback_identity_and_link_are_cached_for_twenty_four_hours():
    state = run(Client())
    original = deepcopy(state)
    client = Client(helix=AssertionError("Cached identity must not query Helix"), twitch_pages=[], steam_pages=[])

    refreshed = run(client, state=state, now=NOW + timedelta(hours=23))

    assert client.calls == [] and state == original
    row = refreshed["games"][TWITCH_ID]
    assert row["checked_at"] == AT and row["steam_appids"] == [APPID]
    assert refreshed["report"]["lookup_count"] == 0


@pytest.mark.parametrize("status", ["pending", "no_steam_link"])
@pytest.mark.parametrize("old_version", [None, 1])
def test_legacy_negative_policy_is_retried_immediately_once_without_waiting_old_retry(status, old_version):
    state = legacy_state(status)
    if old_version is not None:
        state["games"][TWITCH_ID]["lookup_policy_version"] = old_version
    original = deepcopy(state)
    client = Client()

    refreshed = run(client, state=state)

    assert state == original and client.calls
    row = refreshed["games"][TWITCH_ID]
    assert row["status"] == "matched" and row["checked_at"] == AT
    next_client = Client(helix=AssertionError("Policy migration must not force every hourly lookup"),
                         twitch_pages=[], steam_pages=[])
    cached = run(next_client, state=refreshed, now=NOW + timedelta(hours=1))
    assert next_client.calls == [] and cached["games"][TWITCH_ID]["checked_at"] == AT


def test_failed_new_identity_proof_preserves_previous_confirmed_link_and_retries_next_hour():
    state = run(Client())
    original = deepcopy(state)
    failed = Client(twitch_pages=[requests.ConnectionError("Authorization Bearer SECRET")], steam_pages=[])
    later = NOW + timedelta(days=1)

    updated = run(failed, state=state, now=later)

    assert state == original
    row = updated["games"][TWITCH_ID]
    assert row["status"] == "matched" and row["steam_appids"] == [APPID]
    assert row["igdb_id"] == IGDB_ID and row["checked_at"] == AT
    assert row["igdb_identity"] == original["games"][TWITCH_ID]["igdb_identity"]
    assert row["retry_at"] == original["games"][TWITCH_ID]["retry_at"]
    assert "SECRET" not in json.dumps(updated)
    good = Client()
    recovered = run(good, state=updated, now=later + timedelta(hours=1))
    assert good.calls and recovered["games"][TWITCH_ID]["checked_at"] == "2026-10-04T15:00:00Z"


def test_failed_policy_upgrade_does_not_hide_legacy_negative_until_old_retry_deadline():
    state = legacy_state("no_steam_link")
    failed = Client(twitch_pages=[requests.ConnectionError("temporary identity outage")], steam_pages=[])

    updated = run(failed, state=state)

    row = updated["games"][TWITCH_ID]
    assert row["checked_at"] == FIRST and row["steam_appids"] == []
    good = Client()
    recovered = run(good, state=updated, now=NOW + timedelta(hours=1))
    assert good.calls and recovered["games"][TWITCH_ID]["status"] == "matched"
    assert recovered["games"][TWITCH_ID]["checked_at"] == "2026-10-03T15:00:00Z"


def test_successful_identity_with_no_steam_link_is_cached_as_a_completed_policy_decision():
    initial = run(Client(steam_pages=[[]]))
    row = initial["games"][TWITCH_ID]
    assert row["status"] == "no_steam_link" and row["igdb_id"] == IGDB_ID
    assert row["checked_at"] == AT and row["steam_appids"] == []
    cached_client = Client(helix=AssertionError("Completed negative policy decision must be cached"),
                           twitch_pages=[], steam_pages=[])

    cached = run(cached_client, state=initial, now=NOW + timedelta(hours=1))

    assert cached_client.calls == []
    assert cached["games"][TWITCH_ID]["status"] == "no_steam_link"
    assert cached["games"][TWITCH_ID]["checked_at"] == AT


def test_partial_twitch_identity_pages_cannot_commit_first_page_or_try_steam():
    pages = [[twitch_link(id=index + 1) for index in range(500)],
             requests.ConnectionError("partial external page SECRET")]
    client = Client(twitch_pages=pages, steam_pages=[])

    state = run(client)

    row = state["games"][TWITCH_ID]
    assert row["status"] != "matched" and row["checked_at"] is None
    assert row["steam_appids"] == [] and "SECRET" not in json.dumps(state)
    assert not any(endpoint == "external_games" and f"external_game_source = {STEAM_SOURCE}" in query
                   for endpoint, query in client.calls)


@pytest.mark.parametrize("links", [
    [twitch_link(game=999)],
    [twitch_link(), twitch_link(id=501, game=999)],
])
def test_completed_conflicting_uid_proof_invalidates_previous_positive_identity(links):
    initial = run(Client())
    original = deepcopy(initial)
    client = Client(twitch_pages=[links], steam_pages=[])

    updated = run(client, state=initial, now=NOW + timedelta(days=1))

    assert initial == original
    row = updated["games"][TWITCH_ID]
    assert row["status"] == "pending" and row["igdb_id"] is None
    assert row["steam_appids"] == [] and row["links"] == []
    assert row["public_steam_appids"] == [] and row["missing_public_appids"] == []
    assert "igdb_identity" not in row and "related_steam_identity" not in row
    assert row["checked_at"] == "2026-10-04T14:00:00Z"


@pytest.mark.parametrize("change", [
    lambda source: source["enrollment"].update(viewer_count=6999),
    lambda source: source["enrollment"].update(qualification="unverified"),
    lambda source: source.update(status="excluded"),
    lambda source: source.update(release_at="2026-09-01T00:00:00Z"),
])
def test_fallback_cannot_admit_unqualified_or_expired_tracking_source(change):
    registry = tracking()
    change(registry["games"][TWITCH_ID]["tracking_sources"]["twitch_new"])
    client = Client(helix=AssertionError("Unqualified source must not query Helix"),
                    twitch_pages=[], steam_pages=[])

    state = run(client, registry=registry)

    assert client.calls == [] and state["games"] == {}
