"""Verified public Steam metadata can label a cached official Twitch identity."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from collectors.steam_twitch_mapping import (
    METHOD, normalize_steam_catalog, refresh_mappings,
)


NOW = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
APPID = "2638890"
TWITCH_ID = "327598602"
IGDB_ID = "325602"
STORE_DAY = "2026-09-03"
REAL_INSTANT = "2026-09-04T04:02:14Z"
OBSERVED = "2026-10-02T09:00:00Z"
CHECKED = "2026-10-03T11:10:00Z"
AUTHORITY = "steam_taiwan_store_date_authoritative"
REVERSE_METHOD = "twitch_igdb_external_steam_v1"


def enrollment():
    return {"source": "twitch_directory_dom", "viewer_count": 9000,
            "min_viewers": 7000, "observed_at": OBSERVED}


def authoritative_game():
    return {
        "appid": int(APPID), "name": "Onimusha: Way of the Sword",
        "name_en": "Onimusha: Way of the Sword", "display_name": "鬼武者 Way of the Sword",
        "release_start": STORE_DAY, "release_end": STORE_DAY,
        "release_store_date": STORE_DAY, "release_precision": "day",
        "release_time_utc": REAL_INSTANT, "release_display_precision": "date_full",
        "release_date_timezone": "Asia/Taipei", "release_timestamp_taipei_date": "2026-09-04",
        "release_date_conflict": True, "release_date_normalization": AUTHORITY,
        "release_display_provider": "Steam Store appdetails cc=TW l=tchinese",
        "release_date_verified_at": CHECKED, "followers": 0,
        "steam_type": "game", "sexual_content_screened": True,
        "follower_checked_at": CHECKED,
        "tags": ["Action"], "tag_labels_zh_tw": {"Action": "動作"},
        "genres": ["Adventure"], "genre_labels_zh_tw": {"Adventure": "冒險"},
        "twitch_admission": {
            "schema_version": 1, "method": REVERSE_METHOD, "appid": int(APPID),
            "twitch_game_id": TWITCH_ID, "igdb_id": IGDB_ID, "checked_at": CHECKED,
            "source_frontend_commit": "a" * 40, "source_enrollment": enrollment(),
        },
    }


def catalog(*games):
    return {"version": 2, "generated_at": CHECKED,
            "count": len(games), "games": list(games)}


def tracking():
    source = {"source": "twitch_new", "status": "active", "first_seen_at": OBSERVED,
              "release_at": None, "expires_at": None, "enrollment": enrollment()}
    entry = {"game_id": TWITCH_ID, "game_name": "A different Twitch spelling",
             "igdb_id": IGDB_ID, "box_art_url": "https://static-cdn.jtvnw.net/ttv-boxart/test-{width}x{height}.jpg",
             "first_seen_at": OBSERVED, "updated_at": CHECKED, "status": "active",
             "enrollment": enrollment(), "tracking_sources": {"twitch_new": source}}
    return {"schema_version": 1, "updated_at": CHECKED, "games": {TWITCH_ID: entry}}


def discoveries():
    row = {
        "twitch_game_id": TWITCH_ID, "twitch_name": "A different Twitch spelling", "igdb_id": IGDB_ID,
        "status": "matched", "method": REVERSE_METHOD, "active": True,
        "steam_appids": [APPID], "public_steam_appids": [APPID], "missing_public_appids": [],
        "first_seen_at": OBSERVED, "updated_at": CHECKED, "checked_at": CHECKED,
        "retry_at": "2026-10-04T11:10:00Z", "twitch_enrollment": enrollment(),
        "links": [{"external_game_id": "12345", "external_game_source": "45", "uid": APPID,
                   "game": IGDB_ID, "steam_appid": APPID,
                   "url": f"https://store.steampowered.com/app/{APPID}/"}],
    }
    return {"schema_version": 1, "updated_at": CHECKED, "steam_source_id": "45",
            "games": {TWITCH_ID: row}}


class NoNetwork:
    """Any IGDB, Helix, or authentication access is a test failure."""
    def __getattr__(self, name):
        raise AssertionError(f"Cached identity must not access network client: {name}")


def remap(public=None, mapping=None, reverse=None, registry=None):
    return refresh_mappings(
        NoNetwork(), public if public is not None else catalog(authoritative_game()),
        mapping_state=mapping, now=NOW,
        discovery_state=discoveries() if reverse is None else reverse,
        tracking_state=tracking() if registry is None else registry,
        allow_lookup=False,
    )


def set_path(row, path, value):
    for key in path[:-1]:
        row = row[key]
    row[path[-1]] = value


def test_verified_taiwan_store_day_preserves_chinese_metadata_without_reenrollment():
    source = catalog(authoritative_game())
    before = deepcopy(source)

    game = normalize_steam_catalog(source, NOW)[0]

    assert source == before
    assert game["steam_appid"] == APPID and game["display_name"] == "鬼武者 Way of the Sword"
    assert game["name_en"] == "Onimusha: Way of the Sword"
    assert game["release_date"] == STORE_DAY
    assert game["release_at"] == "2026-09-02T16:00:00Z"
    assert game["expires_at"] == "2026-10-02T16:00:00Z"
    assert game["release_time_basis"] == AUTHORITY and game["is_recent"] is False
    assert game["release_time_utc"] == REAL_INSTANT
    assert game["release_timestamp_taipei_date"] == "2026-09-04"
    assert game["release_date_conflict"] is True and game["release_store_date"] == STORE_DAY
    assert game["tag_labels_zh_tw"] == {"Action": "動作"}
    assert source["games"][0]["release_time_utc"] == REAL_INSTANT


@pytest.mark.parametrize("path,value", [
    (("twitch_admission",), None),
    (("twitch_admission", "appid"), 999),
    (("twitch_admission", "method"), "name_similarity"),
    (("twitch_admission", "twitch_game_id"), None),
    (("twitch_admission", "igdb_id"), None),
    (("twitch_admission", "source_frontend_commit"), "main"),
    (("twitch_admission", "checked_at"), "2026-10-03T11:10:00"),
    (("twitch_admission", "source_enrollment", "source"), "steam_recent_release"),
    (("twitch_admission", "source_enrollment", "viewer_count"), 6999),
    (("twitch_admission", "source_enrollment", "min_viewers"), 5000),
    (("twitch_admission", "source_enrollment", "observed_at"), "2026-10-04T00:00:00Z"),
    (("release_display_provider",), "Steam IStoreBrowseService/GetItems"),
    (("release_date_verified_at",), None),
    (("release_date_normalization",), "steam_store_date_matches_taipei"),
    (("release_timestamp_taipei_date",), STORE_DAY),
    (("release_date_conflict",), False),
    (("release_store_date",), "2026-09-02"),
    (("release_time_utc",), "2026-09-04T04:02:14"),
    (("release_display_precision",), "month"),
    (("release_date_timezone",), "UTC"),
    (("steam_type",), "dlc"),
    (("sexual_content_screened",), False),
    (("followers",), -1),
    (("followers",), True),
    (("follower_checked_at",), None),
])
def test_date_conflict_requires_complete_admission_and_official_taiwan_audit(path, value):
    game = authoritative_game()
    set_path(game, path, value)

    assert normalize_steam_catalog(catalog(game), NOW) == []


def test_normal_matching_date_still_uses_original_exact_release_instant():
    game = authoritative_game()
    game.pop("twitch_admission")
    game.update(release_start="2026-09-04", release_end="2026-09-04",
                release_date_conflict=False, release_date_normalization="steam_store_date_matches_taipei")

    normalized = normalize_steam_catalog(catalog(game), NOW)[0]

    assert normalized["release_at"] == REAL_INSTANT
    assert normalized["release_time_basis"] == "exact_utc"
    assert normalized["display_name"] == game["display_name"]


def test_store_authority_recent_window_ends_at_taiwan_day_midnight():
    end = datetime(2026, 10, 2, 16, tzinfo=timezone.utc)
    source = catalog(authoritative_game())

    assert normalize_steam_catalog(source, end - timedelta(seconds=1))[0]["is_recent"] is True
    assert normalize_steam_catalog(source, end)[0]["is_recent"] is False


def test_current_reverse_identity_supplies_public_chinese_metadata_without_api_calls():
    reverse, registry, public = discoveries(), tracking(), catalog(authoritative_game())
    before = deepcopy((reverse, registry, public))

    updated = remap(public=public, reverse=reverse, registry=registry)

    assert (reverse, registry, public) == before
    row = updated["games"][APPID]
    assert row["status"] == "matched" and row["method"] == METHOD
    assert row["twitch_game_id"] == TWITCH_ID and row["igdb_id"] == IGDB_ID
    assert row["checked_at"] == CHECKED
    assert row["steam"]["display_name"] == "鬼武者 Way of the Sword"
    assert row["steam"]["is_recent"] is False
    assert updated["report"]["lookup_count"] == 0


def test_cached_reverse_identity_refreshes_chinese_name_from_current_public_catalog():
    mapping = remap()
    game = authoritative_game()
    game.update(display_name="鬼武者：劍之道", followers=12000)

    updated = remap(public=catalog(game), mapping=mapping)

    row = updated["games"][APPID]
    assert row["checked_at"] == CHECKED
    assert row["steam"]["display_name"] == "鬼武者：劍之道"
    assert row["steam"]["followers"] == 12000
    assert row["steam"]["is_recent"] is False


def test_reverse_discovery_cannot_publish_metadata_missing_from_current_public_catalog():
    updated = remap(public=catalog())

    assert updated["games"] == {}
    assert updated["report"]["catalog_count"] == 0


@pytest.mark.parametrize("mutate", [
    lambda state: state["games"][TWITCH_ID].update(status="excluded"),
    lambda state: state["games"][TWITCH_ID].update(igdb_id="999"),
    lambda state: state["games"][TWITCH_ID]["tracking_sources"]["twitch_new"].update(status="excluded"),
    lambda state: state["games"][TWITCH_ID]["tracking_sources"]["twitch_new"].update(release_at="2026-09-01T00:00:00Z"),
    lambda state: state["games"][TWITCH_ID]["tracking_sources"]["twitch_new"]["enrollment"].update(viewer_count=7000),
])
def test_stale_or_mismatched_current_tracking_cannot_seed_reverse_link(mutate):
    registry = tracking()
    mutate(registry)

    updated = remap(registry=registry)

    assert updated["games"][APPID]["status"] != "matched"
    assert not updated["games"][APPID].get("twitch_game_id")


def test_inactive_reverse_decision_does_not_seed_current_identity():
    reverse = discoveries()
    reverse["games"][TWITCH_ID]["active"] = False

    assert remap(reverse=reverse)["games"][APPID]["status"] != "matched"


@pytest.mark.parametrize("field,value", [
    ("external_game_source", "1"), ("game", "999"), ("uid", "999"),
    ("url", "https://example.invalid/app/2638890/"),
])
def test_reverse_external_evidence_must_fail_closed_on_identity_or_source_mismatch(field, value):
    reverse = discoveries()
    reverse["games"][TWITCH_ID]["links"][0][field] = value
    before = deepcopy(reverse)

    with pytest.raises(ValueError):
        remap(reverse=reverse)

    assert reverse == before


def test_old_negative_forward_decision_can_be_replaced_by_newer_confirmed_reverse_identity():
    normalized = normalize_steam_catalog(catalog(authoritative_game()), NOW)[0]
    mapping = {"schema_version": 1, "updated_at": "2026-10-03T10:00:00Z", "steam_source_id": "45",
               "games": {APPID: {"steam_appid": APPID, "status": "unmatched", "method": METHOD,
                                 "steam": normalized, "checked_at": "2026-10-03T10:00:00Z",
                                 "retry_at": "2026-10-04T10:00:00Z", "reason": "no_igdb_steam_link"}}}

    row = remap(mapping=mapping)["games"][APPID]

    assert row["status"] == "matched" and row["checked_at"] == CHECKED
    assert row["twitch_game_id"] == TWITCH_ID and row["steam"]["display_name"] == "鬼武者 Way of the Sword"


def test_two_confirmed_reverse_categories_for_one_appid_are_not_guessed():
    reverse, registry = discoveries(), tracking()
    alternate = "999999999"
    reverse["games"][alternate] = deepcopy(reverse["games"][TWITCH_ID])
    reverse["games"][alternate].update(twitch_game_id=alternate, igdb_id="999")
    reverse["games"][alternate]["links"][0].update(game="999", external_game_id="67890")
    registry["games"][alternate] = deepcopy(registry["games"][TWITCH_ID])
    registry["games"][alternate].update(game_id=alternate, igdb_id="999")

    updated = remap(reverse=reverse, registry=registry)

    assert updated["games"][APPID]["status"] != "matched"
    assert not updated["games"][APPID].get("twitch_game_id")


@pytest.mark.parametrize("status", ["unmatched", "ambiguous"])
@pytest.mark.parametrize("checked", [CHECKED, "2026-10-03T11:30:00Z"])
def test_equal_or_newer_forward_decision_is_not_overwritten_by_reverse_cache(status, checked):
    normalized = normalize_steam_catalog(catalog(authoritative_game()), NOW)[0]
    mapping = {"schema_version": 1, "updated_at": "2026-10-03T11:30:00Z", "steam_source_id": "45",
               "games": {APPID: {"steam_appid": APPID, "status": status, "method": METHOD,
                                 "steam": normalized, "checked_at": checked,
                                 "retry_at": "2026-10-04T11:30:00Z", "reason": "newer_forward_decision"}}}

    updated = remap(mapping=mapping)

    row = updated["games"][APPID]
    assert row["status"] == status and row["checked_at"] == checked
    assert row["reason"] == "newer_forward_decision"


@pytest.mark.parametrize("checked", ["2026-10-03T10:00:00Z", "2026-10-03T11:30:00Z"])
def test_confirmed_forward_identity_is_preserved_when_reverse_chain_conflicts(checked):
    mapping = remap()
    mapping["games"][APPID].update(twitch_game_id="111", igdb_id="222", checked_at=checked)

    row = remap(mapping=mapping)["games"][APPID]

    assert row["status"] == "matched"
    assert row["twitch_game_id"] == "111" and row["igdb_id"] == "222"
    assert row["checked_at"] == checked
