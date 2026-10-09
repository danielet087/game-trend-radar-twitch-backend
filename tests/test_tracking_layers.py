"""Membership behavior shared by pure rules, canonical composition, and legacy CLI callers."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib

import pytest

from radar_backend.domain.twitch_newness import timestamp

NOW = datetime(2026, 9, 29, 16, 0, tzinfo=timezone.utc)
OWNERS = (
    "radar_backend.domain.twitch_tracking",
    "radar_backend.adapters.twitch_tracking",
    "collectors.twitch_tracking",
)


@pytest.fixture(params=OWNERS)
def owner(request):
    return importlib.import_module(request.param)


def observation(*, viewers=8000, game_id="1", release=None, at=NOW):
    row = {
        "game_id": game_id,
        "game_name": "Tracked game",
        "viewer_count": viewers,
        "verification": {
            "status": "new",
            "observed_at": timestamp(at),
            "expires_at": timestamp(at + timedelta(hours=24)),
        },
    }
    if release is not None:
        row["release_evidence"] = {
            "first_release_date": timestamp(release),
            "checked_at": timestamp(at),
        }
    return row


def populated(owner, *, release=None, at=NOW):
    state = owner.normalize_tracking_state(None, at)
    owner.enroll_observation(state, observation(release=release, at=at), at)
    return state


def steam_mapping(*, release=NOW - timedelta(days=3), game_id="1", appid="100"):
    return {
        "status": "matched",
        "twitch_game_id": game_id,
        "steam_appid": appid,
        "twitch_name": "Steam tracked game",
        "igdb_id": "101",
        "steam": {
            "steam_appid": appid,
            "release_at": timestamp(release),
            "name": "Steam game",
            "tags": ["Action"],
        },
    }


@pytest.mark.parametrize(
    "viewers,admitted",
    [
        (True, False),
        (False, False),
        ("8000", False),
        (None, False),
        (6999, False),
        (7000, True),
        (7000.0, True),
        (float("nan"), False),
        (float("inf"), True),
    ],
)
def test_enrollment_keeps_original_numeric_policy(owner, viewers, admitted):
    state = owner.normalize_tracking_state(None, NOW)
    row = observation(viewers=viewers)
    entry = owner.enroll_observation(state, row, NOW)
    assert (entry is not None) is admitted
    assert bool(state["games"]) is admitted
    if admitted:
        assert entry is state["games"]["1"]
        assert entry["enrollment"]["viewer_count"] == viewers


@pytest.mark.parametrize("game_id", ["", "x", "1.0", None])
def test_invalid_identity_does_not_add_membership(owner, game_id):
    state = owner.normalize_tracking_state(None, NOW)
    before = deepcopy(state)
    row = observation(game_id=game_id)
    assert owner.enroll_observation(state, row, NOW) is None
    assert state == before and "tracking" not in row


def test_observation_mutation_and_independent_copies(owner):
    state = owner.normalize_tracking_state(None, NOW)
    row = observation(release=NOW - timedelta(days=3))
    row["steam_matches"] = [{"steam_appid": "100", "tags": ["Action"]}]
    entry = owner.enroll_observation(state, row, NOW)
    assert entry is state["games"]["1"]
    assert row["observation_at"] == timestamp(NOW)
    assert row["observation_status"] == "current"
    assert row["observation_freshness"] == "fresh"
    row["steam_matches"][0]["tags"].append("Changed")
    row["tracking"]["enrollment"]["viewer_count"] = 1
    assert entry["steam_matches"][0]["tags"] == ["Action"]
    assert entry["last_observation"]["steam_matches"][0]["tags"] == ["Action"]
    assert entry["enrollment"]["viewer_count"] == 8000


@pytest.mark.parametrize("viewers", [0, 9, 8000])
def test_existing_membership_retains_real_observation_without_new_admission(owner, viewers):
    state = populated(owner)
    row = observation(viewers=viewers)
    row["verification"]["status"] = "not_new"
    entry = owner.enroll_observation(state, row, NOW, allow_twitch_enrollment=False)
    assert entry is state["games"]["1"]
    assert entry["status"] == "active"
    assert entry["last_observation"]["viewer_count"] == viewers


@pytest.mark.parametrize("allow", [False, True])
def test_expired_badge_is_tested_at_explicit_eligibility_time(owner, allow):
    state = owner.normalize_tracking_state(None, NOW)
    row = observation(at=NOW - timedelta(days=1))
    entry = owner.enroll_observation(
        state,
        row,
        NOW,
        eligibility_at=NOW - timedelta(hours=1),
        allow_twitch_enrollment=allow,
    )
    assert (entry is not None) is allow
    if allow:
        assert entry["enrollment"]["observed_at"] == timestamp(NOW)


@pytest.mark.parametrize(
    "days,active", [(-1, False), (0, True), (29, True), (30, False), (31, False)]
)
def test_steam_release_membership_window(owner, days, active):
    state = owner.normalize_tracking_state(None, NOW)
    mapping = steam_mapping(release=NOW - timedelta(days=days))
    before = deepcopy(mapping)
    entry = owner.enroll_steam_mapping(state, mapping, NOW)
    assert (entry is not None) is active
    assert mapping == before
    if active:
        assert entry is state["games"]["1"]
        assert set(entry["tracking_sources"]) == {"steam:100"}
        entry["tracking_sources"]["steam:100"]["steam"]["tags"].append("Local")
        assert mapping == before


def test_expired_twitch_and_active_steam_form_one_membership(owner):
    state = populated(owner, release=NOW - timedelta(days=30), at=NOW - timedelta(days=1))
    entry = owner.enroll_steam_mapping(state, steam_mapping(), NOW)
    assert entry["status"] == "active"
    assert entry["tracking_sources"]["twitch_new"]["status"] == "expired"
    assert entry["tracking_sources"]["steam:100"]["status"] == "active"
    assert entry["release_at"] == timestamp(NOW - timedelta(days=30))
    assert entry["expires_at"] == timestamp(NOW + timedelta(days=27))


def test_unknown_twitch_release_survives_steam_expiry(owner):
    state = populated(owner)
    entry = owner.enroll_steam_mapping(state, steam_mapping(release=NOW - timedelta(days=29)), NOW)
    assert owner.reconcile_tracking_entry(entry, NOW + timedelta(days=1)) is entry
    assert entry["status"] == "active" and entry["expires_at"] is None
    assert entry["tracking_sources"]["steam:100"]["status"] == "expired"


@pytest.mark.parametrize("explicit", [False, True])
def test_permanent_exclusion_applies_to_all_sources(owner, explicit):
    state = populated(owner)
    entry = owner.enroll_steam_mapping(state, steam_mapping(), NOW)
    if explicit:
        entry.update(status="excluded", status_reason="curated")
    owner.reconcile_tracking_entry(entry, NOW, non_game_ids=() if explicit else ("1",))
    assert entry["status"] == "excluded"
    assert all(source["status"] == "excluded" for source in entry["tracking_sources"].values())
    assert entry["status_reason"] == ("curated" if explicit else "non_game_category")
    assert owner.enroll_steam_mapping(state, steam_mapping(), NOW) is None
    assert owner.enroll_observation(state, observation(), NOW) is None


def test_twitch_correction_cannot_override_igdb_authority(owner):
    state = populated(owner, release=NOW - timedelta(days=3))
    entry = state["games"]["1"]
    owner.reconcile_tracking_entry(
        entry,
        NOW,
        release_at=timestamp(NOW + timedelta(days=5)),
        release_source="twitch_original_release_date",
    )
    assert entry["release_at"] == timestamp(NOW - timedelta(days=3))
    assert entry["release_source"] == "igdb_first_release_date"
    owner.reconcile_tracking_entry(
        entry,
        NOW,
        release_at=timestamp(NOW + timedelta(days=6)),
        release_source="igdb_first_release_date",
    )
    assert entry["release_at"] == timestamp(NOW + timedelta(days=6))


def test_release_selection_falls_through_invalid_sources(owner):
    row = {
        "release_evidence": {"first_release_date": "invalid"},
        "release_experiment": {
            "igdb_first_release_date": {"release_at": "missing timezone"},
            "twitch_original_release_date": {"release_at": timestamp(NOW)},
        },
    }
    assert owner.release_from_observation(row) == (timestamp(NOW), "twitch_original_release_date")


@pytest.mark.parametrize("offset,admitted", [(-1, False), (0, True), (23, True), (24, False)])
def test_igdb_admission_metadata_window(owner, offset, admitted):
    row = observation(release=NOW - timedelta(days=20))
    row["verification"]["status"] = "pending"
    observed_at = NOW + timedelta(hours=offset)
    assert (owner.admission_evidence(row, observed_at) is not None) is admitted


def test_catalog_removal_excludes_only_steam_and_preserves_twitch(owner):
    state = populated(owner)
    entry = owner.enroll_steam_mapping(state, steam_mapping(), NOW)
    assert owner.reconcile_steam_catalog(state, [], {"games": {}}, NOW) is None
    assert entry["status"] == "active"
    assert entry["tracking_sources"]["steam:100"]["status_reason"] == "steam_not_in_catalog"
    assert entry["steam_matches"] == []


def test_confirmed_catalog_remapping_moves_only_steam_membership(owner):
    state = populated(owner)
    mapping = steam_mapping()
    owner.reconcile_steam_catalog(state, [mapping["steam"]], {"games": {"100": mapping}}, NOW)
    moved = steam_mapping(game_id="2")
    owner.reconcile_steam_catalog(state, [moved["steam"]], {"games": {"100": moved}}, NOW)
    assert state["games"]["1"]["status"] == "active"
    assert (
        state["games"]["1"]["tracking_sources"]["steam:100"]["status_reason"]
        == "steam_mapping_changed"
    )
    assert state["games"]["2"]["tracking_sources"]["steam:100"]["status"] == "active"


def test_legacy_migration_copies_evidence_and_normalization_never_mutates_input(owner):
    state = populated(owner)
    state["games"]["1"].pop("tracking_sources")
    state["games"]["1"]["enrollment"]["kind"] = "manual_restore"
    before = deepcopy(state)
    clean = owner.normalize_tracking_state(state, NOW)
    assert state == before
    clean["games"]["1"]["tracking_sources"]["twitch_new"]["enrollment"]["kind"] = "changed"
    assert clean["games"]["1"]["enrollment"]["kind"] == "manual_restore"
    assert state == before


@pytest.mark.parametrize(
    "change,message",
    [
        ({"schema_version": 2}, "Invalid Twitch tracking state"),
        ({"games": []}, "Invalid Twitch tracking state"),
        ({"updated_at": None}, "requires an update timestamp"),
        ({"updated_at": "invalid"}, "Invalid isoformat string"),
    ],
)
def test_normalization_error_order(owner, change, message):
    state = populated(owner)
    state.update(change)
    with pytest.raises(ValueError, match=message):
        owner.normalize_tracking_state(state, NOW)


def test_normalize_resolves_current_reconciliation_callback(owner, monkeypatch):
    state = populated(owner)
    calls = []

    def reconcile(entry, clock, *, non_game_ids):
        calls.append((entry, clock, non_game_ids))
        entry["patched"] = True

    monkeypatch.setattr(owner, "reconcile_tracking_entry", reconcile)
    result = owner.normalize_tracking_state(state, NOW, non_game_ids=("26936",))
    assert calls == [(result["games"]["1"], NOW, ("26936",))]
    assert result["games"]["1"]["patched"] is True
    assert "patched" not in state["games"]["1"]


def test_observation_resolves_current_admission_and_metadata_callbacks(owner, monkeypatch):
    calls = []
    monkeypatch.setattr(
        owner, "admission_evidence", lambda row, clock: calls.append(clock) or "patched_source"
    )
    monkeypatch.setattr(owner, "tracking_metadata", lambda entry: {"patched": entry["game_id"]})
    state = owner.normalize_tracking_state(None, NOW)
    row = observation()
    row.pop("verification")
    entry = owner.enroll_observation(state, row, NOW)
    assert entry["enrollment"]["source"] == "patched_source"
    assert row["tracking"] == {"patched": "1"}
    assert calls == [NOW]


def test_current_tracking_window_and_status_constants(owner, monkeypatch):
    monkeypatch.setattr(owner, "TRACKING_DAYS", 1)
    state = owner.normalize_tracking_state(None, NOW)
    assert (
        owner.enroll_steam_mapping(state, steam_mapping(release=NOW - timedelta(days=2)), NOW)
        is None
    )
    entry = owner.enroll_observation(state, observation(), NOW)
    monkeypatch.setattr(owner, "STATUSES", {"patched"})
    with pytest.raises(ValueError, match="identity or status"):
        owner.normalize_tracking_state(state, NOW)
    assert entry["status"] == "active"


def test_explicit_domain_ports_accept_falsy_callable_objects():
    from radar_backend.domain import twitch_tracking as rules

    class FalsyClockFormatter:
        def __bool__(self):
            return False

        def __call__(self, value):
            return "patched_timestamp"

    assert (
        rules.normalize_tracking_state(None, NOW, timestamp_fn=FalsyClockFormatter())["updated_at"]
        == "patched_timestamp"
    )
    calls = []

    class FalsyAdmission:
        def __bool__(self):
            return False

        def __call__(self, row, clock):
            calls.append(clock)
            return None

    state = rules.normalize_tracking_state(None, NOW)
    assert (
        rules.enroll_observation(state, observation(), NOW, admission_evidence_fn=FalsyAdmission())
        is None
    )
    assert calls == [NOW]
