from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import pytest
import requests

from collectors.twitch_steam_discovery import METHOD, normalize_discovery_state, refresh_discoveries

NOW = datetime(2026, 10, 2, 9, tzinfo=timezone.utc)
AT = "2026-10-02T09:00:00Z"
FIRST = "2026-09-29T14:00:00Z"


def tracking(*ids, viewer_count=9000, enrollment_source="igdb_first_release_date", **source_changes):
    games = {}
    for twitch_id in ids or (300,):
        twitch_id = str(twitch_id)
        evidence = {"source": enrollment_source, "viewer_count": viewer_count, "min_viewers": 7000,
                    "observed_at": FIRST}
        source = {"source": "twitch_new", "status": "active", "first_seen_at": FIRST,
                  "release_at": "2026-09-29T00:00:00Z", "release_source": "igdb_first_release_date",
                  "enrollment": evidence, **source_changes}
        games[twitch_id] = {"game_id": twitch_id, "game_name": f"Twitch title {twitch_id}",
                           "first_seen_at": FIRST, "status": "active", "updated_at": AT,
                           "enrollment": deepcopy(evidence), "tracking_sources": {"twitch_new": source}}
    return {"schema_version": 1, "updated_at": AT, "games": games}


def catalog(*ids):
    return {"version": 2, "count": len(ids), "generated_at": "2026-10-02T02:00:00Z",
            "games": [{"appid": appid} for appid in ids]}


def category(twitch_id=300, igdb_id=20, name="Entirely different title"):
    return {"id": str(twitch_id), "igdb_id": str(igdb_id) if igdb_id is not None else "", "name": name}


def external(appid=100, igdb_id=20, source=45, external_id=1):
    return {"id": external_id, "uid": str(appid), "game": igdb_id, "external_game_source": source,
            "url": "https://malicious.invalid/ignored"}


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        if isinstance(self.payload, Exception):
            raise self.payload

    def json(self):
        return self.payload


class Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.session = self
        self.client_id, self.access_token, self.timeout_seconds = "client", "token", 20
        self.calls = []

    def _wait(self):
        pass

    def authenticate(self):
        self.access_token = "token"

    def get(self, endpoint, *, params):
        self.calls.append((endpoint, params))
        expected, payload = self.responses.pop(0)
        assert expected == endpoint
        if isinstance(payload, Exception):
            raise payload
        return payload

    def post(self, url, **kwargs):
        endpoint = url.rsplit("/", 1)[1]
        self.calls.append((endpoint, kwargs["data"]))
        expected, payload = self.responses.pop(0)
        assert expected == endpoint
        return Response(payload)


def first_responses(*links):
    responses = [("games", {"data": [category()]}),
                 ("external_game_sources", [{"id": 1, "name": "GOG"}, {"id": 45, "name": "sTeAm"}]),
                 ("external_games", list(links))]
    if not links:
        responses.append(("games", [{"id": 20, "websites": []}]))
    return responses


def matched_state(*appids):
    return refresh_discoveries(Client(first_responses(*[external(appid, external_id=index + 1)
                                                      for index, appid in enumerate(appids or (100,))])),
                               tracking(), catalog(), now=NOW)


def test_reverse_link_uses_fresh_helix_and_dynamic_steam_source_without_name_matching():
    client = Client(first_responses(external(source=1, appid=999), external(igdb_id=21, appid=888), external()))
    state = refresh_discoveries(client, tracking(), catalog(), now=NOW)
    row = state["games"]["300"]
    assert row["status"] == "matched" and row["method"] == METHOD
    assert row["igdb_id"] == "20" and row["steam_appids"] == ["100"]
    assert row["twitch_name"] == "Entirely different title"
    assert row["links"] == [{"external_game_id": "1", "external_game_source": "45", "uid": "100",
                             "game": "20", "steam_appid": "100", "url": "https://store.steampowered.com/app/100/"}]
    assert client.calls[0] == ("games", [("id", "300")])
    assert "external_game_source = 45 & game = (20)" in client.calls[2][1]
    assert "category" not in client.calls[2][1] and "search" not in client.calls[2][1]
    assert "viewer_count" not in row and "followers" not in json.dumps(row)
    assert row["checked_at"] == AT and row["first_seen_at"] == FIRST


def test_multiple_official_steam_products_and_duplicate_external_pages_are_preserved():
    state = refresh_discoveries(Client(first_responses(external(), external(), external(101, external_id=2),
                                                       external(100, external_id=3))), tracking(), catalog(100), now=NOW)
    row = state["games"]["300"]
    assert row["status"] == "matched" and row["steam_appids"] == ["100", "101"]
    assert len(row["links"]) == 3
    assert row["public_steam_appids"] == ["100"] and row["missing_public_appids"] == ["101"]
    assert state["source_catalog"]["appids"] == ["100"]
    assert state["report"]["missing_public_appids"] == ["101"]


def test_already_public_links_still_cached_and_metadata_membership_refreshes_without_requests():
    state = matched_state()
    before = deepcopy(state)
    client = Client([])
    updated = refresh_discoveries(client, tracking(), catalog(100), state, NOW + timedelta(hours=1))
    assert state == before and client.calls == []
    row = updated["games"]["300"]
    assert row["checked_at"] == AT and row["first_seen_at"] == FIRST
    assert row["public_steam_appids"] == ["100"] and row["missing_public_appids"] == []
    assert row["updated_at"] == "2026-10-02T10:00:00Z"
    assert updated["source_catalog"]["appids"] == ["100"]


def test_no_steam_link_is_not_a_not_steam_assertion_and_is_rechecked_after_twenty_four_hours():
    state = refresh_discoveries(Client(first_responses()), tracking(), catalog(), now=NOW)
    row = state["games"]["300"]
    assert row["status"] == "no_steam_link" and row["reason"] == "no_igdb_steam_link"
    assert row["steam_appids"] == [] and row["links"] == []
    assert row["retry_at"] == "2026-10-03T09:00:00Z"
    cached = refresh_discoveries(Client([]), tracking(), catalog(), state, NOW + timedelta(hours=23))
    assert cached["games"]["300"]["checked_at"] == AT
    client = Client([("games", {"data": [category()]}), ("external_games", [external()])])
    refreshed = refresh_discoveries(client, tracking(), catalog(), state, NOW + timedelta(hours=24))
    assert refreshed["games"]["300"]["status"] == "matched"


@pytest.mark.parametrize("categories", [[], [category(igdb_id=None)]])
def test_missing_twitch_or_igdb_identity_is_pending_without_steam_assertion(categories):
    responses = [("games", {"data": categories})]
    if categories:
        responses.extend([("external_game_sources", [{"id": 77, "name": "Twitch"}]),
                          ("external_games", [])])
    client = Client(responses)
    row = refresh_discoveries(client, tracking(), catalog(), now=NOW)["games"]["300"]
    assert row["status"] == "pending" and row["checked_at"] is None
    assert row["reason"] == "missing_twitch_igdb_identity" and row["steam_appids"] == []
    assert len(client.calls) == (3 if categories else 1)


@pytest.mark.parametrize("stage", ["helix", "source", "external"])
def test_api_errors_have_no_new_checked_timestamp_or_secret_text(stage):
    secret = requests.ConnectionError("Authorization Bearer SECRET store credentials")
    if stage == "helix":
        responses = [("games", secret)]
    elif stage == "source":
        responses = [("games", {"data": [category()]}), ("external_game_sources", secret)]
    else:
        responses = first_responses()
        responses[2] = ("external_games", secret)
    result = refresh_discoveries(Client(responses), tracking(), catalog(), now=NOW)
    row = result["games"]["300"]
    assert row["status"] == "unavailable" and row["checked_at"] is None and row["retry_at"] is None
    assert result["report"]["status"] == "unavailable_or_partial"
    assert "SECRET" not in json.dumps(result) and "Authorization" not in json.dumps(result)


def test_fresh_conflicting_owner_invalidates_old_link_even_if_steam_lookup_fails_and_retries_next_hour():
    state = matched_state()
    client = Client([("games", {"data": [category(igdb_id=21)]}),
                     ("external_games", requests.ConnectionError("SECRET"))])
    later = NOW + timedelta(days=1)
    updated = refresh_discoveries(client, tracking(), catalog(100), state, later)
    row = updated["games"]["300"]
    assert row["status"] == "unavailable" and row["igdb_id"] is None
    assert row["steam_appids"] == [] and row["links"] == []
    assert not row.get("igdb_identity") and not row.get("related_steam_identity")
    assert row["checked_at"] == "2026-10-03T09:00:00Z"
    client = Client([("games", {"data": [category(igdb_id=21)]}),
                     ("external_games", [external(101, igdb_id=21)])])
    updated = refresh_discoveries(client, tracking(), catalog(100), updated, later + timedelta(hours=1))
    assert client.calls
    assert updated["games"]["300"]["steam_appids"] == ["101"]
    assert updated["games"]["300"]["missing_public_appids"] == ["101"]


def test_missing_fresh_identity_does_not_drop_confirmed_cache_or_forge_new_checked_at():
    state = matched_state()
    updated = refresh_discoveries(Client([("games", {"data": []})]), tracking(), catalog(), state,
                                  NOW + timedelta(days=1))
    assert updated["games"]["300"]["status"] == "matched"
    assert updated["games"]["300"]["checked_at"] == AT
    assert updated["games"]["300"]["steam_appids"] == ["100"]


def test_steam_only_expired_excluded_and_unverified_manual_entries_cannot_create_reverse_source():
    payload = tracking(300, 301, 302, 303, 304, 305)
    games = payload["games"]
    games["301"]["tracking_sources"] = {"steam:100": {"source": "steam_recent_release", "steam_appid": "100",
        "status": "active", "release_at": "2026-09-29T00:00:00Z", "enrollment": deepcopy(games["301"]["enrollment"])}}
    games["302"]["tracking_sources"]["twitch_new"]["release_at"] = "2026-09-01T00:00:00Z"
    games["303"]["status"] = "excluded"
    games["304"]["tracking_sources"]["twitch_new"]["enrollment"].update(
        source="user_requested_legacy_recovery", qualification="unverified", viewer_count=None)
    games["305"]["tracking_sources"]["twitch_new"]["enrollment"]["viewer_count"] = 6999
    client = Client(first_responses(external()))
    result = refresh_discoveries(client, payload, catalog(), now=NOW)
    assert set(result["games"]) == {"300"} and client.calls[0][1] == [("id", "300")]


def test_original_admission_not_current_low_viewers_controls_discovery_and_expired_cache_is_retained():
    state = matched_state()
    payload = tracking()
    # This actual latest observation is below the enrollment threshold.
    payload["games"]["300"]["last_observation"] = {"game_id": "300", "observation_at": AT, "viewer_count": 0}
    result = refresh_discoveries(Client([]), payload, catalog(), state, NOW + timedelta(hours=1))
    assert result["games"]["300"]["active"] is True
    expired = refresh_discoveries(Client([]), payload, catalog(), result, NOW + timedelta(days=31))
    assert expired["games"]["300"]["active"] is False
    assert expired["games"]["300"]["steam_appids"] == ["100"]
    assert expired["report"]["active_twitch_games"] == 0


@pytest.mark.parametrize("value", [False, True, 7000.0, None, "7000"])
def test_enrollment_cannot_be_unverified_or_fake_threshold_type(value):
    result = refresh_discoveries(Client([]), tracking(viewer_count=value), catalog(), now=NOW)
    assert result["games"] == {}


def test_future_enrollment_and_non_game_categories_are_excluded():
    source = tracking(300, 26936)
    source["games"]["300"]["tracking_sources"]["twitch_new"]["enrollment"]["observed_at"] = "2026-10-03T00:00:00Z"
    assert refresh_discoveries(Client([]), source, catalog(), now=NOW)["games"] == {}


def test_helix_batches_at_most_100_and_igdb_all_pages_are_complete():
    payload = tracking(*range(1, 206))
    responses = []
    for start in range(1, 206, 100):
        ids = range(start, min(start + 100, 206))
        responses.append(("games", {"data": [category(i, i + 1000) for i in ids]}))
        if start == 1:
            responses.append(("external_game_sources", [{"id": 45, "name": "Steam"}]))
        responses.append(("external_games", [external(i + 2000, igdb_id=i + 1000, external_id=i) for i in ids]))
    client = Client(responses)
    result = refresh_discoveries(client, payload, catalog(), now=NOW)
    assert len(result["games"]) == 205
    assert [len(params) for endpoint, params in client.calls if endpoint == "games"] == [100, 100, 5]
    responses = first_responses(*[external(external_id=i + 1) for i in range(500)])
    responses.append(("external_games", [external(101, external_id=501)]))
    client = Client(responses)
    result = refresh_discoveries(client, tracking(), catalog(), now=NOW)
    assert result["games"]["300"]["steam_appids"] == ["100", "101"]
    assert "offset 500;" in client.calls[-1][1]


def test_partial_page_failure_does_not_commit_first_page_or_advance_identity_timestamp():
    state = matched_state()
    client = Client([("games", {"data": [category()]}),
                     ("external_games", [external(999, external_id=i + 1) for i in range(500)]),
                     ("external_games", requests.ConnectionError("secret"))])
    updated = refresh_discoveries(client, tracking(), catalog(), state, NOW + timedelta(days=1))
    assert updated["games"]["300"]["steam_appids"] == ["100"]
    assert updated["games"]["300"]["checked_at"] == AT


def test_deadline_prevents_even_first_request_and_does_not_create_false_no_link():
    client = Client([])
    result = refresh_discoveries(client, tracking(), catalog(), now=NOW, deadline=10, monotonic=lambda: 10)
    assert client.calls == [] and result["report"]["deadline_exhausted"] is True
    assert result["games"]["300"]["status"] == "unavailable"
    assert result["games"]["300"]["checked_at"] is None


@pytest.mark.parametrize("responses", [
    [("games", {"data": {}})],
    [("games", {"data": [category(), category(igdb_id=21)]})],
    [("games", {"data": [category()]}), ("external_game_sources", [{"id": 1, "name": "GOG"}])],
    first_responses(external(appid="nonnumeric")),
    first_responses(external(), external(101)),
])
def test_malformed_or_conflicting_authoritative_responses_fail_closed(responses):
    result = refresh_discoveries(Client(responses), tracking(), catalog(), now=NOW)
    assert result["games"]["300"]["status"] == "unavailable"
    assert result["games"]["300"]["steam_appids"] == []
    assert result["games"]["300"]["checked_at"] is None


@pytest.mark.parametrize("mutate", [
    lambda state: state["games"]["300"].update(method="name_similarity"),
    lambda state: state["games"]["300"].update(steam_appids=["100", "100"]),
    lambda state: state["games"]["300"]["links"][0].update(external_game_source="1"),
    lambda state: state["games"]["300"]["links"][0].update(game="21"),
    lambda state: state["games"]["300"]["links"][0].update(uid="101"),
    lambda state: state["games"]["300"]["links"][0].update(url="https://fake.invalid/"),
    lambda state: state["games"]["300"].update(links=[]),
    lambda state: state["games"]["300"].update(checked_at=None),
    lambda state: state["games"]["300"].update(twitch_game_id="301"),
    lambda state: state["games"]["300"].update(status="pending"),
    lambda state: state["games"]["300"].update(public_steam_appids=["100"], missing_public_appids=["100"]),
    lambda state: state["source_catalog"].update(count=999),
    lambda state: state["source_catalog"].update(count=1, appids=["100"]),
    lambda state: state.update(schema_version=True),
])
def test_persisted_registry_rejects_unproven_or_inconsistent_links(mutate):
    state = matched_state()
    mutate(state)
    with pytest.raises(ValueError):
        normalize_discovery_state(state)


def test_invalid_public_catalog_never_silently_means_all_appids_missing():
    source = catalog(100)
    source["count"] = 2
    with pytest.raises(ValueError):
        refresh_discoveries(Client([]), tracking(), source, now=NOW)
    source = catalog(100, 100)
    with pytest.raises(ValueError):
        refresh_discoveries(Client([]), tracking(), source, now=NOW)
    assert normalize_discovery_state() == {"schema_version": 1, "updated_at": None, "games": {}}
