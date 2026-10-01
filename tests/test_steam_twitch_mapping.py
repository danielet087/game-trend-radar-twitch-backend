from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import pytest
import requests

from collectors.steam_twitch_mapping import (
    METHOD, normalize_mapping_state, normalize_steam_catalog, refresh_mappings,
)

NOW = datetime(2026, 10, 1, 8, tzinfo=timezone.utc)


def steam(appid=100, **changes):
    return {"appid": appid, "name": "Graveyard Keeper 2", "display_name": "守墓人 2", "name_en": "Graveyard Keeper 2",
        "release_precision": "day", "release_start": "2026-09-20", "release_end": "2026-09-20",
        "release_time_utc": "2026-09-20T15:00:00Z", "release_date_timezone": "Asia/Taipei",
        "followers": 5200, "tags": ["Adventure", "Adventure"], "genres": ["Indie"],
        "tag_labels_zh_tw": {"Adventure": "冒險", "Other": "不應沿用"}, "genre_labels_zh_tw": {"Indie": "獨立"},
        "header_image": "https://steam.invalid/header.jpg", **changes}


def catalog(*rows):
    games = list(rows) or [steam()]
    return {"version": 2, "count": len(games), "generated_at": "2026-10-01T02:15:40Z", "games": games}


class Response:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        if isinstance(self.data, Exception):
            raise self.data

    def json(self):
        return self.data


class Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.session = self
        self.client_id, self.access_token = "client", "token"
        self.timeout_seconds = 20
        self.calls = []

    def _wait(self):
        pass

    def authenticate(self):
        self.access_token = "token"

    def post(self, url, **kwargs):
        endpoint = url.rsplit("/", 1)[1]
        self.calls.append((endpoint, kwargs["data"]))
        expected, payload = self.responses.pop(0)
        assert endpoint == expected
        return Response(payload)

    def get(self, endpoint, *, params):
        self.calls.append((endpoint, params))
        expected, payload = self.responses.pop(0)
        assert endpoint == expected
        if isinstance(payload, Exception):
            raise payload
        return payload


def external(appid=100, igdb=20, source=45, identity=1):
    return {"id": identity, "uid": str(appid), "game": igdb, "external_game_source": source}


def twitch(igdb=20, identity=300):
    return {"id": str(identity), "name": "Graveyard Keeper II", "igdb_id": str(igdb),
        "box_art_url": "https://static-cdn.jtvnw.net/ttv-boxart/300-{width}x{height}.jpg"}


def linked_state():
    return refresh_mappings(Client([("external_game_sources", [{"id": 45, "name": "Steam"}]),
        ("external_games", [external()]), ("games", {"data": [twitch()]})]), catalog(), now=NOW)


def test_catalog_uses_exact_utc_release_preserves_only_compact_store_metadata():
    source = catalog()
    before = deepcopy(source)
    row = normalize_steam_catalog(source, NOW)[0]
    assert source == before
    assert row["release_at"] == "2026-09-20T15:00:00Z"
    assert row["expires_at"] == "2026-10-20T15:00:00Z"
    assert row["release_precision"] == "day" and row["release_time_basis"] == "exact_utc"
    assert row["is_recent"] is True
    assert row["display_name"] == "守墓人 2"
    assert row["tags"] == ["Adventure"] and row["tag_labels_zh_tw"] == {"Adventure": "冒險"}
    assert not any("image" in key or "art" in key for key in row)


def test_taipei_day_only_and_release_instant_boundaries():
    row = steam(release_time_utc=None, release_start="2026-10-02", release_end="2026-10-02")
    assert normalize_steam_catalog(catalog(row), NOW)[0]["is_recent"] is False
    entry = normalize_steam_catalog(catalog(row), datetime(2026, 10, 1, 16, tzinfo=timezone.utc))[0]
    assert entry["release_at"] == "2026-10-01T16:00:00Z" and entry["is_recent"] is True
    assert entry["release_precision"] == "day" and entry["release_time_basis"] == "taipei_date_midnight"
    release = datetime(2026, 9, 20, 15, tzinfo=timezone.utc)
    assert normalize_steam_catalog(catalog(), release - timedelta(seconds=1))[0]["is_recent"] is False
    assert normalize_steam_catalog(catalog(), release)[0]["is_recent"] is True
    assert normalize_steam_catalog(catalog(), release + timedelta(days=30))[0]["is_recent"] is False


@pytest.mark.parametrize("changes", [
    {"release_precision": "month"}, {"release_end": "2026-09-21"}, {"release_date_conflict": True},
    {"release_time_utc": "2026-09-20T16:00:00Z"}, {"release_time_utc": "bad"},
    {"release_date_timezone": "UTC"}, {"release_timestamp_taipei_date": "2026-09-21"},
])
def test_uncertain_or_conflicting_dates_cannot_enter_recent_steam(changes):
    assert normalize_steam_catalog(catalog(steam(**changes)), NOW) == []


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(version=1), lambda p: p.update(count=999), lambda p: p.update(generated_at="bad"),
    lambda p: p["games"].append(deepcopy(p["games"][0])),
])
def test_invalid_catalog_is_not_silently_empty(mutate):
    source = catalog()
    mutate(source)
    if len(source["games"]) == 2:
        source["count"] = 2
    with pytest.raises(ValueError):
        normalize_steam_catalog(source, NOW)


def test_authoritative_different_names_uses_dynamic_source_not_deprecated_category():
    client = Client([("external_game_sources", [{"id": 1, "name": "GOG"}, {"id": 45, "name": "sTeAm"}]),
        ("external_games", [external(source=1, igdb=99), external()]), ("games", {"data": [twitch()]})])
    state = refresh_mappings(client, catalog(), now=NOW)
    row = state["games"]["100"]
    assert row["status"] == "matched" and row["method"] == METHOD
    assert row["igdb_id"] == "20" and row["twitch_game_id"] == "300"
    assert row["twitch_name"] == "Graveyard Keeper II" and row["steam"]["name_en"] == "Graveyard Keeper 2"
    assert "jtvnw.net" in row["box_art_url"]
    assert "external_game_source = 45" in client.calls[1][1] and "category" not in client.calls[1][1]
    assert client.calls[2][1] == [("igdb_id", "20")]


def test_multiple_igdb_or_twitch_candidates_are_not_automatically_chosen():
    client = Client([("external_game_sources", [{"id": 45, "name": "Steam"}]),
        ("external_games", [external(), external(igdb=21, identity=2)]),
        # No Helix lookup: the AppID itself is already ambiguous.
    ])
    result = refresh_mappings(client, catalog(), now=NOW)["games"]["100"]
    assert result["status"] == "ambiguous" and result["candidates"] == ["20", "21"]
    assert "twitch_game_id" not in result
    client = Client([("external_game_sources", [{"id": 45, "name": "Steam"}]),
        ("external_games", [external()]), ("games", {"data": [twitch(), twitch(identity=301)]})])
    result = refresh_mappings(client, catalog(), now=NOW)["games"]["100"]
    assert result["status"] == "ambiguous" and result["candidates"] == ["300", "301"]
    assert "twitch_game_id" not in result


def test_no_category_or_no_igdb_link_is_unmatched_without_fuzzy_fallback():
    client = Client([("external_game_sources", [{"id": 45, "name": "Steam"}]),
        ("external_games", [external()]), ("games", {"data": []})])
    row = refresh_mappings(client, catalog(), now=NOW)["games"]["100"]
    assert row["status"] == "unmatched" and row["reason"] == "no_twitch_category"
    assert row["igdb_id"] == "20" and "viewer_count" not in row
    client = Client([("external_game_sources", [{"id": 45, "name": "Steam"}]), ("external_games", [])])
    row = refresh_mappings(client, catalog(), now=NOW)["games"]["100"]
    assert row["status"] == "unmatched" and row["reason"] == "no_igdb_steam_link"
    assert len(client.calls) == 2


def test_confirmed_ids_cached_metadata_changes_and_expiry_still_recomputed():
    state = linked_state()
    original = deepcopy(state)
    client = Client([])
    updated = refresh_mappings(client, catalog(steam(followers=9000, display_name="新名稱")), state, NOW + timedelta(days=35))
    assert client.calls == [] and state == original
    row = updated["games"]["100"]
    assert row["twitch_game_id"] == "300" and row["steam"]["followers"] == 9000
    assert row["steam"]["display_name"] == "新名稱" and row["steam"]["is_recent"] is False


def test_removed_catalog_game_keeps_identity_but_cannot_reenroll_as_recent():
    state = linked_state()
    source = {"version": 2, "count": 0, "generated_at": "2026-10-01T02:15:40Z", "games": []}
    updated = refresh_mappings(Client([]), source, state, NOW + timedelta(hours=1))
    row = updated["games"]["100"]
    assert row["twitch_game_id"] == "300" and row["steam"]["is_recent"] is False
    assert row["metadata_updated_at"] == "2026-10-01T09:00:00Z"


def test_unmatched_retry_after_twenty_four_hours_and_outage_preserves_previous_link():
    client = Client([("external_game_sources", [{"id": 45, "name": "Steam"}]), ("external_games", [])])
    state = refresh_mappings(client, catalog(), now=NOW)
    cached = refresh_mappings(Client([]), catalog(), state, NOW + timedelta(hours=23))
    assert cached["games"]["100"]["checked_at"] == state["games"]["100"]["checked_at"]
    client = Client([("external_games", [external()]), ("games", {"data": [twitch()]})])
    updated = refresh_mappings(client, catalog(), state, NOW + timedelta(hours=24))
    assert updated["games"]["100"]["status"] == "matched"
    client = Client([("external_games", requests.ConnectionError("Bearer SECRET"))])
    updated = refresh_mappings(client, catalog(steam(), steam(101)), updated, NOW + timedelta(hours=25))
    assert updated["games"]["100"]["twitch_game_id"] == "300"
    assert updated["games"]["101"]["status"] == "pending"
    assert "SECRET" not in json.dumps(updated) and updated["report"]["status"] == "unavailable_or_partial"


def test_missing_dynamic_source_never_assumes_steam_is_one():
    client = Client([("external_game_sources", [{"id": 1, "name": "GOG"}])])
    state = refresh_mappings(client, catalog(), now=NOW)
    assert state["games"]["100"]["status"] == "pending"
    assert state["report"]["status"] == "unavailable_or_partial" and len(client.calls) == 1


def test_igdb_external_game_pages_continued_past_500_and_helix_batches_at_most_100():
    # Duplicate records for one exact AppID can fill a whole IGDB page.
    responses = [("external_game_sources", [{"id": 45, "name": "Steam"}]),
        ("external_games", [external(identity=i + 1) for i in range(500)]),
        ("external_games", [external(101, 21, identity=501)]),
        ("games", {"data": [twitch(), twitch(21, 301)]})]
    client = Client(responses)
    state = refresh_mappings(client, catalog(steam(), steam(101)), now=NOW)
    assert "offset 500;" in client.calls[2][1]
    assert state["games"]["101"]["twitch_game_id"] == "301"
    rows = [steam(i) for i in range(1, 206)]
    responses = [("external_game_sources", [{"id": 45, "name": "Steam"}])]
    for start in range(1, 206, 100):
        ids = range(start, min(start + 100, 206))
        responses.extend([("external_games", [external(i, i, identity=i) for i in ids]),
            ("games", {"data": [twitch(i, i + 1000) for i in ids]})])
    client = Client(responses)
    state = refresh_mappings(client, catalog(*rows), now=NOW)
    assert len(state["games"]) == 205
    assert [len(params) for endpoint, params in client.calls if endpoint == "games"] == [100, 100, 5]


def test_malformed_api_page_preserves_prior_unmatched_status_and_deadline_prevents_requests():
    client = Client([("external_game_sources", [{"id": 45, "name": "Steam"}]), ("external_games", [])])
    state = refresh_mappings(client, catalog(), now=NOW)
    checked = state["games"]["100"]["checked_at"]
    client = Client([("external_games", [external()]), ("games", {"data": {}})])
    updated = refresh_mappings(client, catalog(), state, NOW + timedelta(days=1))
    assert updated["games"]["100"]["status"] == "unmatched" and updated["games"]["100"]["checked_at"] == checked
    client = Client([])
    updated = refresh_mappings(client, catalog(), now=NOW, deadline=10, monotonic=lambda: 10)
    assert client.calls == [] and updated["report"]["status"] == "unavailable_or_partial"


def test_mapping_registry_rejects_unproven_confirmed_ids():
    assert normalize_mapping_state(None) == {"schema_version": 1, "updated_at": None, "games": {}}
    state = linked_state()
    state["games"]["100"]["method"] = "name_similarity"
    with pytest.raises(ValueError):
        normalize_mapping_state(state)
