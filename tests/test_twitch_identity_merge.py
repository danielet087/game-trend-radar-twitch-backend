"""Publication races preserve independent completed identity decisions."""
from copy import deepcopy

import pytest

from scripts.store_twitch_snapshot import merge_discovery_state
from tests.test_twitch_steam_website_identity import APPID, IGDB_ID, TWITCH_ID, proof


FIRST = "2026-10-02T09:00:00Z"
T15 = "2026-10-03T15:00:00Z"
T16 = "2026-10-03T16:00:00Z"
T17 = "2026-10-03T17:00:00Z"
T18 = "2026-10-03T18:00:00Z"
T19 = "2026-10-03T19:00:00Z"


def registry(*, checked=T15, updated=T17, igdb=IGDB_ID, version=2):
    row = {
        "twitch_game_id": TWITCH_ID, "twitch_name": "TCG Card Shop Simulator",
        "igdb_id": igdb, "status": "no_steam_link", "method": "twitch_igdb_external_steam_v1",
        "steam_appids": [], "links": [], "public_steam_appids": [], "missing_public_appids": [],
        "first_seen_at": FIRST, "checked_at": checked, "retry_at": "2026-10-04T15:00:00Z",
        "updated_at": updated, "active": True, "lookup_policy_version": version,
        "twitch_enrollment": {"source": "twitch_directory_dom", "viewer_count": 9000,
                              "min_viewers": 7000, "observed_at": FIRST},
    }
    return {"schema_version": 1, "updated_at": updated, "steam_source_id": "45",
            "twitch_source_id": "77", "games": {TWITCH_ID: row}}


def positive(*, canonical=T15, related=T16, updated=T17, label="TCG 中文商店名稱"):
    state = registry(checked=canonical, updated=updated)
    evidence = proof()
    evidence["checked_at"] = related
    evidence["steam_identity_metadata"].update(checked_at=related, display_name=label)
    state["games"][TWITCH_ID].update(
        related_steam_identity=evidence, related_lookup_status="matched", related_checked_at=related,
        related_retry_at="2026-10-04T16:00:00Z",
    )
    return state


def negative(*, canonical=T15, related=T17, updated=T18):
    state = registry(checked=canonical, updated=updated)
    state["games"][TWITCH_ID].update(
        related_lookup_status="no_steam_website", related_checked_at=related,
        related_retry_at="2026-10-04T17:00:00Z",
    )
    return state


def merged_row(old, new, *, reverse=False):
    before = deepcopy((old, new))
    merged = merge_discovery_state(new, old) if reverse else merge_discovery_state(old, new)
    assert (old, new) == before
    return merged["games"][TWITCH_ID]


@pytest.mark.parametrize("reverse", [False, True])
def test_fresher_metadata_without_optional_proof_cannot_erase_same_owner_website_identity(reverse):
    old = positive()
    newer = registry(checked=T17, updated=T18)

    row = merged_row(old, newer, reverse=reverse)

    assert row["checked_at"] == T17 and row["updated_at"] == T18
    assert row["related_steam_identity"] == old["games"][TWITCH_ID]["related_steam_identity"]
    assert row["related_checked_at"] == T16 and row["related_lookup_status"] == "matched"


@pytest.mark.parametrize("reverse", [False, True])
def test_newest_website_proof_wins_independently_of_older_canonical_check(reverse):
    newer_canonical = positive(canonical=T17, related=T16, updated=T18, label="舊中文名")
    newer_related = positive(canonical=T15, related=T18, updated=T19, label="最新中文名")

    row = merged_row(newer_canonical, newer_related, reverse=reverse)

    assert row["checked_at"] == T17
    assert row["related_checked_at"] == T18
    assert row["related_steam_identity"]["steam_identity_metadata"]["display_name"] == "最新中文名"


@pytest.mark.parametrize("reverse", [False, True])
def test_new_completed_website_negative_removes_old_positive_proof(reverse):
    old = positive(canonical=T17, related=T16, updated=T17)
    newer_negative = negative(canonical=T15, related=T17, updated=T18)

    row = merged_row(old, newer_negative, reverse=reverse)

    assert row["checked_at"] == T17
    assert row["related_lookup_status"] == "no_steam_website"
    assert row["related_checked_at"] == T17
    assert "related_steam_identity" not in row


@pytest.mark.parametrize("reverse", [False, True])
def test_old_completed_negative_cannot_erase_newer_website_proof(reverse):
    old_negative = negative(canonical=T17, related=T16, updated=T18)
    newer_positive = positive(canonical=T15, related=T17, updated=T17)

    row = merged_row(old_negative, newer_positive, reverse=reverse)

    assert row["related_lookup_status"] == "matched" and row["related_checked_at"] == T17
    assert row["related_steam_identity"] == newer_positive["games"][TWITCH_ID]["related_steam_identity"]


@pytest.mark.parametrize("reverse", [False, True])
def test_new_api_failure_keeps_prior_valid_website_proof_and_real_check_time(reverse):
    old = positive()
    failed = registry(checked=T17, updated=T18)
    failed["games"][TWITCH_ID].update(related_lookup_status="unavailable", related_retry_at=None)

    row = merged_row(old, failed, reverse=reverse)

    assert row["related_steam_identity"] == old["games"][TWITCH_ID]["related_steam_identity"]
    assert row["related_checked_at"] == T16
    assert row["related_lookup_status"] == "unavailable" and row["related_retry_at"] is None


@pytest.mark.parametrize("reverse", [False, True])
def test_older_outage_cannot_override_a_newer_completed_website_decision(reverse):
    failed = registry(checked=T17, updated=T17)
    failed["games"][TWITCH_ID].update(related_lookup_status="unavailable", related_retry_at=None)
    newer = positive(canonical=T15, related=T18, updated=T19)

    row = merged_row(failed, newer, reverse=reverse)

    assert row["related_lookup_status"] == "matched" and row["related_checked_at"] == T18


@pytest.mark.parametrize("kind", ["changed_owner", "direct_match", "invalidated_owner"])
@pytest.mark.parametrize("reverse", [False, True])
def test_new_canonical_owner_or_direct_match_cannot_inherit_old_related_identity(kind, reverse):
    old = positive()
    newer = registry(checked=T17, updated=T18, igdb="999" if kind == "changed_owner" else IGDB_ID)
    row = newer["games"][TWITCH_ID]
    if kind == "direct_match":
        row.update(status="matched", steam_appids=[APPID], missing_public_appids=[APPID],
                   links=[{"external_game_id": "600", "external_game_source": "45", "uid": APPID,
                           "game": IGDB_ID, "steam_appid": APPID,
                           "url": f"https://store.steampowered.com/app/{APPID}/"}])
    elif kind == "invalidated_owner":
        row.update(status="pending", igdb_id=None)

    merged = merged_row(old, newer, reverse=reverse)

    assert merged["checked_at"] == T17
    assert not any(key in merged for key in (
        "related_steam_identity", "related_lookup_status", "related_checked_at", "related_retry_at",
    ))


@pytest.mark.parametrize("key", ["steam_source_id", "twitch_source_id"])
@pytest.mark.parametrize("reverse", [False, True])
def test_missing_global_source_in_new_metadata_cannot_erase_known_official_source(key, reverse):
    known = registry()
    fresh_empty = {"schema_version": 1, "updated_at": T18, key: None, "games": {}}

    merged = merge_discovery_state(fresh_empty, known) if reverse else merge_discovery_state(known, fresh_empty)

    assert merged[key] == known[key]


@pytest.mark.parametrize("key", ["steam_source_id", "twitch_source_id"])
def test_conflicting_global_sources_cannot_be_silently_combined(key):
    known = registry()
    conflict = {"schema_version": 1, "updated_at": T18, key: "999", "games": {}}

    with pytest.raises(ValueError):
        merge_discovery_state(known, conflict)


@pytest.mark.parametrize("reverse", [False, True])
def test_same_identity_policy_version_cannot_regress_under_newer_legacy_metadata(reverse):
    modern = registry(checked=T15, updated=T17, version=2)
    legacy = registry(checked=T17, updated=T18, version=1)

    row = merged_row(modern, legacy, reverse=reverse)

    assert row["checked_at"] == T17 and row["lookup_policy_version"] == 2
