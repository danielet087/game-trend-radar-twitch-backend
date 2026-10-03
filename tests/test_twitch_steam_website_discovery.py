"""Independent store identities must not admit old Steam products or hide retries."""
from copy import deepcopy
from datetime import timedelta
import json

import pytest
import requests

from collectors.twitch_steam_discovery import normalize_discovery_state, refresh_discoveries
from collectors.twitch_steam_website_identity import METHOD, PROVIDER
from tests.test_twitch_steam_discovery import AT, NOW, Client, catalog, category, first_responses, tracking


def website_identity(at=AT, igdb_id="20"):
    url = "https://store.steampowered.com/app/292030/"
    return {"method": METHOD, "twitch_game_id": "300", "igdb_id": igdb_id, "steam_appid": "292030",
            "website_links": [{"website_id": "808", "game": igdb_id, "steam_appid": "292030",
                               "source_url": url + "The_Witcher_3_Wild_Hunt__Remastered/", "url": url}],
            "checked_at": at, "steam_identity_metadata": {
                "steam_appid": "292030", "steam_type": "game", "display_name": "巫師3：狂獵 — 重製版",
                "store_url": url, "sexual_content_screened": True, "content_descriptor_ids": [1, 5],
                "release_store_date": "2015-05-18", "release_date_raw": "2015 年 5 月 18 日",
                "raw_release_date": {"coming_soon": False, "date": "2015 年 5 月 18 日"},
                "checked_at": at, "provider": PROVIDER}}


def patch_lookup(monkeypatch, function):
    monkeypatch.setattr("collectors.twitch_steam_website_identity.lookup_website_identity", function)


def discovered(monkeypatch):
    patch_lookup(monkeypatch, lambda *args, **kwargs: website_identity())
    return refresh_discoveries(Client(first_responses()), tracking(), catalog(), now=NOW)


def test_store_identity_labels_existing_category_without_claiming_external_chain_or_catalog_membership(monkeypatch):
    state = discovered(monkeypatch)
    row = state["games"]["300"]
    assert row["status"] == "no_steam_link" and row["reason"] == "no_igdb_steam_link"
    assert row["method"] == "twitch_igdb_external_steam_v1"
    assert row["steam_appids"] == row["links"] == row["public_steam_appids"] == row["missing_public_appids"] == []
    assert row["related_steam_identity"]["steam_identity_metadata"]["display_name"] == "巫師3：狂獵 — 重製版"
    assert row["related_steam_identity"]["steam_identity_metadata"]["release_store_date"] == "2015-05-18"
    assert "external_game_id" not in json.dumps(row["related_steam_identity"])
    assert state["report"]["related_lookup_count"] == 1


def test_complete_store_identity_and_negative_canonical_decision_are_cached_without_requests(monkeypatch):
    state = discovered(monkeypatch)
    patch_lookup(monkeypatch, lambda *args, **kwargs: pytest.fail("a 24-hour identity cache must be reused"))
    client = Client([])
    updated = refresh_discoveries(client, tracking(), catalog(), state, NOW + timedelta(hours=23))
    assert client.calls == []
    assert updated["games"]["300"]["related_steam_identity"] == state["games"]["300"]["related_steam_identity"]
    assert updated["report"]["lookup_count"] == updated["report"]["related_lookup_count"] == 0


@pytest.mark.parametrize("failure", [requests.ConnectionError("SECRET token"), ValueError("partial metadata")])
def test_website_outage_retries_next_hour_without_refetching_cached_direct_negative(monkeypatch, failure):
    patch_lookup(monkeypatch, lambda *args, **kwargs: (_ for _ in ()).throw(failure))
    state = refresh_discoveries(Client(first_responses()), tracking(), catalog(), now=NOW)
    row = state["games"]["300"]
    assert row["checked_at"] == AT and row["retry_at"] == "2026-10-03T09:00:00Z"
    assert row["related_lookup_status"] == "unavailable" and row["related_retry_at"] is None
    assert "SECRET" not in json.dumps(state)
    next_at = "2026-10-02T10:00:00Z"
    patch_lookup(monkeypatch, lambda *args, **kwargs: website_identity(next_at))
    client = Client([])
    updated = refresh_discoveries(client, tracking(), catalog(), state, NOW + timedelta(hours=1))
    assert client.calls == [] and updated["report"]["lookup_count"] == 0
    assert updated["report"]["related_lookup_count"] == 1
    assert updated["games"]["300"]["checked_at"] == AT
    assert updated["games"]["300"]["related_steam_identity"]["checked_at"] == next_at


def test_store_outage_preserves_same_owner_proof_without_inventing_new_proof_time(monkeypatch):
    state = discovered(monkeypatch)
    patch_lookup(monkeypatch, lambda *args, **kwargs: (_ for _ in ()).throw(requests.ConnectionError("429")))
    updated = refresh_discoveries(Client([("games", {"data": [category()]}), ("external_games", [])]),
                                  tracking(), catalog(), state, NOW + timedelta(days=1))
    row = updated["games"]["300"]
    assert row["checked_at"] == "2026-10-03T09:00:00Z"
    assert row["related_steam_identity"] == state["games"]["300"]["related_steam_identity"]
    assert row["related_checked_at"] == AT and row["related_retry_at"] is None
    assert row["related_lookup_status"] == "unavailable"


def test_new_completed_website_negative_removes_old_proof(monkeypatch):
    state = discovered(monkeypatch)
    patch_lookup(monkeypatch, lambda *args, **kwargs: None)
    updated = refresh_discoveries(Client([("games", {"data": [category()]}), ("external_games", [])]),
                                  tracking(), catalog(), state, NOW + timedelta(days=1))
    row = updated["games"]["300"]
    assert "related_steam_identity" not in row
    assert row["related_lookup_status"] == "no_steam_website"
    assert row["related_checked_at"] == "2026-10-03T09:00:00Z"


def test_fresh_contradictory_helix_owner_invalidates_old_store_proof_even_when_followup_api_fails(monkeypatch):
    state = discovered(monkeypatch)
    patch_lookup(monkeypatch, lambda *args, **kwargs: pytest.fail("contradicted identity cannot use an old proof"))
    updated = refresh_discoveries(Client([("games", {"data": [category(igdb_id=21)]}),
                                         ("external_games", requests.ConnectionError("temporary"))]),
                                  tracking(), catalog(), state, NOW + timedelta(days=1))
    row = updated["games"]["300"]
    assert row["status"] == "unavailable" and row["igdb_id"] is None
    assert "related_steam_identity" not in row and row["steam_appids"] == []
    assert row["retry_at"] is None


@pytest.mark.parametrize("missing_category", [True, False])
@pytest.mark.parametrize("related_status", ["matched", "unavailable", "no_steam_website"])
def test_completed_missing_current_identity_clears_old_related_proof_without_invalid_mixed_state(monkeypatch, missing_category, related_status):
    state = discovered(monkeypatch)
    state["games"]["300"]["related_lookup_status"] = related_status
    if related_status == "unavailable":
        state["games"]["300"]["related_retry_at"] = None
    if related_status == "no_steam_website":
        state["games"]["300"].pop("related_steam_identity")
    patch_lookup(monkeypatch, lambda *args, **kwargs: pytest.fail("missing current identity cannot request old websites"))
    responses = [("games", {"data": [] if missing_category else [category(igdb_id=None)]})]
    if not missing_category:
        responses.extend([("external_game_sources", [{"id": 77, "name": "Twitch"}]), ("external_games", [])])
    updated = refresh_discoveries(Client(responses), tracking(), catalog(), state, NOW + timedelta(days=1))
    row = updated["games"]["300"]
    assert row["status"] == "pending" and row["reason"] == "missing_twitch_igdb_identity"
    assert row["igdb_id"] is None and row["steam_appids"] == row["links"] == []
    assert all(key not in row for key in ("related_steam_identity", "related_lookup_status", "related_checked_at",
                                         "related_retry_at", "igdb_identity"))
    assert normalize_discovery_state(updated) == updated


@pytest.mark.parametrize("mutate", [
    lambda state: state["games"]["300"]["related_steam_identity"].update(igdb_id="21"),
    lambda state: state["games"]["300"].update(updated_at="2026-10-02T08:00:00Z"),
    lambda state: state["games"]["300"].update(related_lookup_status="no_steam_website"),
    lambda state: state["games"]["300"].update(related_checked_at="2026-10-03T09:00:00Z"),
])
def test_cached_related_identity_rejects_different_owners_future_times_and_negative_proof(monkeypatch, mutate):
    state = discovered(monkeypatch)
    mutate(state)
    with pytest.raises(ValueError):
        normalize_discovery_state(state)


def test_lookup_does_not_mutate_previous_discovery_documents(monkeypatch):
    state = discovered(monkeypatch)
    before = deepcopy(state)
    refresh_discoveries(Client([]), tracking(), catalog(), state, NOW + timedelta(hours=1))
    assert state == before
