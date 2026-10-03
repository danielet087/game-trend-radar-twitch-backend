"""Exact website evidence is independent from external-game/catalog qualification."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest
import requests

from collectors import twitch_steam_website_identity as identity
from collectors.twitch_live import CollectionDeadlineExceeded

NOW = datetime(2026, 10, 3, 16, tzinfo=timezone.utc)
CHECKED = "2026-10-03T16:00:00Z"
TWITCH_ID, IGDB_ID, APPID = "1230742965", "187832", "3070070"
STORE_URL = f"https://store.steampowered.com/app/{APPID}/"


def steam_payload(*, appid=APPID, raw_date="6 Jul, 2015"):
    return {appid: {"success": True, "data": {
        "steam_appid": int(appid), "type": "game", "name": "TCG 中文商店名稱",
        "content_descriptors": {"ids": [1, 2, 5], "notes": "Official descriptors"},
        "release_date": {"coming_soon": False, "date": raw_date},
    }}}


def proof():
    return {"method": identity.METHOD, "twitch_game_id": TWITCH_ID, "igdb_id": IGDB_ID,
            "steam_appid": APPID, "checked_at": CHECKED,
            "website_links": [{"website_id": "456", "game": IGDB_ID, "steam_appid": APPID,
                               "source_url": STORE_URL, "url": STORE_URL}],
            "steam_identity_metadata": {
                "steam_appid": APPID, "steam_type": "game", "display_name": "TCG 中文商店名稱",
                "store_url": STORE_URL, "sexual_content_screened": True, "content_descriptor_ids": [1, 2, 5],
                "release_store_date": "2015-07-06", "release_date_raw": "6 Jul, 2015",
                "raw_release_date": {"coming_soon": False, "date": "6 Jul, 2015"},
                "checked_at": CHECKED, "provider": identity.PROVIDER,
            }}


class Response:
    status_code = 200

    def __init__(self, payload=None, *, status=200):
        self.payload = payload if payload is not None else steam_payload()
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return deepcopy(self.payload)


class ForbiddenClient:
    def __getattr__(self, name):
        raise AssertionError(f"Keyless store lookup cannot access Twitch client credentials: {name}")


def offline(monkeypatch, *, game_rows=None, website_rows=None, store=None, response_status=200):
    games = game_rows if game_rows is not None else [{"id": int(IGDB_ID), "websites": [456]}]
    websites = website_rows if website_rows is not None else [{"id": 456, "game": int(IGDB_ID), "url": STORE_URL}]
    calls, stores = [], []

    def pages(client, endpoint, query, deadline, monotonic):
        assert isinstance(client, ForbiddenClient)
        calls.append((endpoint, query))
        assert endpoint in {"games", "websites"}
        return deepcopy(games if endpoint == "games" else websites)

    def get(url, **kwargs):
        stores.append((url, kwargs))
        return Response(store, status=response_status)

    monkeypatch.setattr(identity, "_pages", pages)
    monkeypatch.setattr(identity.requests, "get", get)
    return calls, stores


def lookup(**kwargs):
    return identity.lookup_website_identity(ForbiddenClient(), TWITCH_ID, IGDB_ID, NOW, **kwargs)


def test_exact_two_level_igdb_binding_and_keyless_store_metadata_preserve_old_release(monkeypatch):
    calls, stores = offline(monkeypatch)

    linked = lookup(deadline=15.0, monotonic=lambda: 10.0)

    assert linked == proof()
    assert calls == [("games", f"fields id,websites; where id = {IGDB_ID};"),
                     ("websites", "fields id,url,game; where id = (456);")]
    assert len(stores) == 1
    url, args = stores[0]
    assert url == identity.APPDETAILS_URL
    assert args["params"] == {"appids": APPID, "cc": "tw", "l": "tchinese"}
    assert args["allow_redirects"] is False
    assert not ({"headers", "auth", "cookies", "data", "json"} & set(args))
    assert args["timeout"].total == 5.0
    metadata = linked["steam_identity_metadata"]
    assert metadata["release_store_date"] == "2015-07-06"
    assert metadata["raw_release_date"]["date"] == "6 Jul, 2015"
    assert metadata["sexual_content_screened"] is True
    assert "followers" not in metadata and "is_recent" not in metadata


@pytest.mark.parametrize("source", [STORE_URL, STORE_URL.rstrip("/"),
                                     STORE_URL + "TCG_Card_Shop_Simulator/",
                                     STORE_URL + "TCG_Card_Shop_Simulator"])
def test_safe_optional_store_slug_keeps_source_url_and_canonical_appid(monkeypatch, source):
    offline(monkeypatch, website_rows=[{"id": 456, "game": int(IGDB_ID), "url": source}])

    linked = lookup()

    assert linked["website_links"][0]["source_url"] == source
    assert linked["website_links"][0]["url"] == STORE_URL
    assert linked["steam_appid"] == APPID


def test_multiple_websites_same_appid_verify_store_only_once(monkeypatch):
    calls, stores = offline(monkeypatch, game_rows=[{"id": int(IGDB_ID), "websites": [900, 456]}],
                           website_rows=[{"id": 900, "game": int(IGDB_ID), "url": STORE_URL + "Name/"},
                                         {"id": 456, "game": int(IGDB_ID), "url": STORE_URL}])

    linked = lookup()

    assert len(stores) == 1
    assert [row["website_id"] for row in linked["website_links"]] == ["456", "900"]
    assert calls[1][1] == "fields id,url,game; where id = (456,900);"


@pytest.mark.parametrize("games,websites", [
    ([{"id": int(IGDB_ID), "websites": []}], []),
    ([{"id": int(IGDB_ID)}], []),
    ([{"id": int(IGDB_ID), "websites": [456]}],
     [{"id": 456, "game": int(IGDB_ID), "url": "https://www.youtube.com/watch?v=123#video"}]),
])
def test_no_steam_website_returns_none_without_store_request(monkeypatch, games, websites):
    calls, stores = offline(monkeypatch, game_rows=games, website_rows=websites)

    assert lookup() is None
    assert stores == []
    assert calls[0][0] == "games"


@pytest.mark.parametrize("games,websites", [
    ([], []),
    ([{"id": 999, "websites": [456]}], []),
    ([{"id": int(IGDB_ID), "websites": [456]}, {"id": int(IGDB_ID), "websites": [456]}], []),
    ([{"id": int(IGDB_ID), "websites": "456"}], []),
    ([{"id": int(IGDB_ID), "websites": [True]}], []),
    ([{"id": int(IGDB_ID), "websites": ["0456"]}], []),
    ([{"id": int(IGDB_ID), "websites": [456, "456"]}], []),
    ([{"id": int(IGDB_ID), "websites": [456]}], []),
    ([{"id": int(IGDB_ID), "websites": [456]}], [{"id": 456, "game": 999, "url": STORE_URL}]),
    ([{"id": int(IGDB_ID), "websites": [456]}], [{"id": 456, "url": STORE_URL}]),
    ([{"id": int(IGDB_ID), "websites": [456]}], [{"id": 789, "game": int(IGDB_ID), "url": STORE_URL}]),
    ([{"id": int(IGDB_ID), "websites": [456]}],
     [{"id": 456, "game": int(IGDB_ID), "url": STORE_URL}, {"id": 456, "game": int(IGDB_ID), "url": STORE_URL}]),
    ([{"id": int(IGDB_ID), "websites": [456, 900]}], [{"id": 456, "game": int(IGDB_ID), "url": STORE_URL}]),
    ([{"id": int(IGDB_ID), "websites": [456, 900]}],
     [{"id": 456, "game": int(IGDB_ID), "url": STORE_URL},
      {"id": 900, "game": int(IGDB_ID), "url": "https://store.steampowered.com/app/999/"}]),
])
def test_partial_ambiguous_or_mismatched_igdb_chain_never_requests_steam(monkeypatch, games, websites):
    _, stores = offline(monkeypatch, game_rows=games, website_rows=websites)

    with pytest.raises(ValueError):
        lookup()

    assert stores == []


@pytest.mark.parametrize("unsafe", [
    "http://store.steampowered.com/app/3070070/",
    "https://store.steampowered.com.evil.test/app/3070070/",
    "https://store.steampowered.com./app/3070070/",
    "https://store.steampowered.com:443/app/3070070/",
    "https://store.steampowered.com:/app/3070070/",
    "https://name:password@store.steampowered.com/app/3070070/",
    "https://store.steampowered.com/app/3070070/?key=secret",
    "https://store.steampowered.com/app/3070070/?",
    "https://store.steampowered.com/app/3070070/#release",
    "https://store.steampowered.com/app/03070070/",
    "https://store.steampowered.com/app/0/",
    "https://store.steampowered.com/sub/3070070/",
    "https://store.steampowered.com/app/3070070/../other/",
    "https://store.steampowered.com/app/3070070/%2E%2E/",
    "https://store.steampowered.com/app/3070070/Name//",
    "https://store.steampowered.com\\@evil.test/app/3070070/",
    "\nhttps://store.steampowered.com/app/3070070/",
    "https://store.steampowered.com/app/3070070/ name",
    "javascript:alert(1)",
])
def test_unsafe_website_metadata_fails_before_fixed_store_request(monkeypatch, unsafe):
    _, stores = offline(monkeypatch, website_rows=[{"id": 456, "game": int(IGDB_ID), "url": unsafe}])

    with pytest.raises(ValueError):
        lookup()

    assert stores == []


@pytest.mark.parametrize("raw_date,expected", [
    ("6 Jul, 2015", "2015-07-06"), ("July 6, 2015", "2015-07-06"),
    ("2015 年 7 月 6 日", "2015-07-06"), ("2015年7月6日", "2015-07-06"),
    ("2015-07-06", "2015-07-06"), ("即將推出", None), ("2026 年", None),
    ("Q4 2026", None), ("31 Feb, 2015", None), ("", None),
])
def test_exact_official_dates_parse_but_uncertain_raw_dates_remain_literal(monkeypatch, raw_date, expected):
    offline(monkeypatch, store=steam_payload(raw_date=raw_date))

    metadata = lookup()["steam_identity_metadata"]

    assert metadata["release_store_date"] == expected
    assert metadata["release_date_raw"] == raw_date
    assert metadata["raw_release_date"]["date"] == raw_date


def change_path(payload, path, value):
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value


@pytest.mark.parametrize("path,value", [
    ((APPID, "success"), False), ((APPID, "success"), 1),
    ((APPID, "data"), None), ((APPID, "data", "steam_appid"), 999),
    ((APPID, "data", "steam_appid"), True), ((APPID, "data", "type"), "dlc"),
    ((APPID, "data", "name"), " "), ((APPID, "data", "release_date"), None),
    ((APPID, "data", "release_date", "coming_soon"), "false"),
    ((APPID, "data", "release_date", "date"), None),
    ((APPID, "data", "content_descriptors"), None),
    ((APPID, "data", "content_descriptors", "ids"), None),
    ((APPID, "data", "content_descriptors", "ids"), [True]),
    ((APPID, "data", "content_descriptors", "ids"), [-1]),
    ((APPID, "data", "content_descriptors", "ids"), ["3"]),
    ((APPID, "data", "content_descriptors", "ids"), [1, 3]),
    ((APPID, "data", "content_descriptors", "ids"), [4]),
])
def test_invalid_or_excluded_official_steam_metadata_cannot_yield_identity(monkeypatch, path, value):
    payload = steam_payload()
    change_path(payload, path, value)
    offline(monkeypatch, store=payload)

    with pytest.raises(ValueError):
        lookup()


@pytest.mark.parametrize("payload", [{}, {"999": {"success": True}},
                                    {**steam_payload(), "999": {"success": True}}])
def test_unrequested_or_missing_store_envelope_is_not_identity(monkeypatch, payload):
    offline(monkeypatch, store=payload)

    with pytest.raises(ValueError):
        lookup()


@pytest.mark.parametrize("status", [301, 302, 429, 500])
def test_redirect_and_http_failure_cannot_yield_identity(monkeypatch, status):
    offline(monkeypatch, response_status=status)

    with pytest.raises((ValueError, requests.HTTPError)):
        lookup()


def test_deadline_exhaustion_never_requests_igdb_or_steam(monkeypatch):
    calls, stores = offline(monkeypatch)

    with pytest.raises(CollectionDeadlineExceeded):
        lookup(deadline=10.0, monotonic=lambda: 10.0)

    assert calls == [] and stores == []


def test_deadline_exhausted_by_store_request_discards_its_response(monkeypatch):
    _, stores = offline(monkeypatch)
    clock = {"value": 1.0}

    def slow_store(url, **kwargs):
        stores.append((url, kwargs))
        clock["value"] = 20.0
        return Response()

    monkeypatch.setattr(identity.requests, "get", slow_store)
    with pytest.raises(CollectionDeadlineExceeded):
        lookup(deadline=10.0, monotonic=lambda: clock["value"])
    assert len(stores) == 1


@pytest.mark.parametrize("path,value", [
    (("method",), "name_similarity"), (("twitch_game_id",), "999"), (("igdb_id",), "999"),
    (("steam_appid",), "999"), (("checked_at",), "2026-10-03T16:00:00"),
    (("website_links", 0, "website_id"), "0456"), (("website_links", 0, "game"), "999"),
    (("website_links", 0, "steam_appid"), "999"), (("website_links", 0, "url"), "https://example.com/"),
    (("website_links", 0, "source_url"), "https://store.steampowered.com/app/999/"),
    (("steam_identity_metadata", "steam_appid"), "999"),
    (("steam_identity_metadata", "steam_type"), "demo"),
    (("steam_identity_metadata", "display_name"), " "),
    (("steam_identity_metadata", "store_url"), "https://example.com/"),
    (("steam_identity_metadata", "sexual_content_screened"), False),
    (("steam_identity_metadata", "content_descriptor_ids"), [3]),
    (("steam_identity_metadata", "content_descriptor_ids"), [4]),
    (("steam_identity_metadata", "content_descriptor_ids"), [False]),
    (("steam_identity_metadata", "content_descriptor_ids"), ["1"]),
    (("steam_identity_metadata", "provider"), "IGDB"),
    (("steam_identity_metadata", "checked_at"), "2026-10-03T17:00:00Z"),
    (("steam_identity_metadata", "release_store_date"), "2015-07-07"),
    (("steam_identity_metadata", "release_date_raw"), "2016 年 7 月 6 日"),
    (("steam_identity_metadata", "raw_release_date", "coming_soon"), 0),
])
def test_persisted_proof_requires_every_identity_screening_and_date_binding(path, value):
    cached = proof()
    change_path(cached, path, value)

    with pytest.raises(ValueError):
        identity.normalize_website_identity(cached, TWITCH_ID, IGDB_ID)


def test_persisted_normalizer_copies_proof_and_rejects_duplicate_or_qualification_fields():
    cached = proof()
    original = deepcopy(cached)
    normalized = identity.normalize_website_identity(cached, TWITCH_ID, IGDB_ID)
    normalized["steam_identity_metadata"]["display_name"] = "Changed copy"
    assert cached == original
    duplicate = deepcopy(cached)
    duplicate["website_links"].append(deepcopy(duplicate["website_links"][0]))
    with pytest.raises(ValueError):
        identity.normalize_website_identity(duplicate, TWITCH_ID, IGDB_ID)
    invented = deepcopy(cached)
    invented["steam_identity_metadata"]["followers"] = 5000
    with pytest.raises(ValueError):
        identity.normalize_website_identity(invented, TWITCH_ID, IGDB_ID)
