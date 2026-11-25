"""Each launch is followed on its own: an Early Access start, the full release
after it, a demo or beta before a known launch. Between launches a game waits,
and the viewer threshold brings it back."""
from datetime import datetime, timedelta, timezone

from collectors.steam_twitch_mapping import normalize_steam_catalog
from collectors.twitch_newness import timestamp
from collectors.twitch_tracking import (
    enroll_observation, enroll_steam_mapping, normalize_tracking_state, reconcile_steam_catalog,
    reconcile_tracking_entry,
)
from radar_backend.domain.steam_twitch_mapping import attach_early_access
from radar_backend.domain.twitch_candidates import apply_release_hint_rows
from radar_backend.domain.twitch_newness import SOURCE_RULES
from radar_backend.domain.twitch_tracking import awaiting_launch
from tests.test_twitch_candidates import NOW, FakeClient, game, page, registry, stream, stub_hints
from tests.test_twitch_tracking import run

EA = NOW - timedelta(days=60)
DAY = timedelta(days=1)


def steam_mapping(early_access=None, release=EA, appid="100"):
    steam = {"steam_appid": appid, "release_at": timestamp(release), "name": "Early game", "tags": []}
    if early_access is not None:
        steam["early_access"] = early_access
    return {"status": "matched", "twitch_game_id": "1", "steam_appid": appid,
            "twitch_name": "Early game", "igdb_id": "101", "steam": steam}


def access(full=None, active=True):
    return {"active": active, "started_at": timestamp(EA), "full_release_at": timestamp(full) if full else None}


def steam_entry(at, early_access):
    state = normalize_tracking_state(None, at)
    entry = enroll_steam_mapping(state, steam_mapping(early_access), at)
    return state, entry


def hint_row(first, rows):
    return {"id": 101, "first_release_date": int(first.timestamp()), "release_dates": rows}


def release_row(moment, fmt="YYYYMMMMDD", status=None, category=None):
    row = {"date": int(moment.timestamp())}
    if fmt:
        row["date_format"] = {"format": fmt}
    if category is not None:
        row["category"] = category
    if status:
        row["status"] = {"name": status}
    return row


def hints_for(row):
    hints = {}
    apply_release_hint_rows([row], NOW, hints, datetime_type=datetime, timezone_type=timezone,
                            timedelta_type=timedelta, source_rules=SOURCE_RULES, timestamp=timestamp)
    return hints["101"]


def test_igdb_year_and_quarter_dates_are_not_launch_days():
    year_end = datetime(2027, 12, 31, tzinfo=timezone.utc)
    assert hints_for(hint_row(year_end, [release_row(year_end, "YYYY")]))["release_precision"] == "year"
    quarter = datetime(2027, 6, 30, tzinfo=timezone.utc)
    assert hints_for(hint_row(quarter, [release_row(quarter, None, category=4)]))["release_precision"] == "quarter"
    exact = datetime(2027, 2, 2, tzinfo=timezone.utc)
    hint = hints_for(hint_row(exact, [release_row(exact), release_row(exact, "YYYY")]))
    assert hint["release_precision"] == "day"
    # Rows read before precision was kept add nothing.
    assert "release_precision" not in hints_for({"id": 101, "first_release_date": int(exact.timestamp())})


def test_igdb_early_access_and_full_release_dates():
    full = datetime(2027, 9, 1, tzinfo=timezone.utc)
    hint = hints_for(hint_row(EA, [release_row(EA, status="Early Access"), release_row(full, status="Full Release"),
                                   release_row(full + DAY * 90, "YYYY", status="Full Release")]))
    assert (hint["early_access_at"], hint["full_release_at"]) == (timestamp(EA), timestamp(full))


def test_steam_early_access_waits_for_its_full_release_and_tracks_it():
    state, entry = steam_entry(EA + DAY, access())
    source = entry["tracking_sources"]["steam:100"]
    assert source["status"] == "active"
    reconcile_tracking_entry(entry, EA + DAY * 31)
    assert (entry["status"], entry["status_reason"]) == ("expired", "awaiting_full_release")
    assert awaiting_launch(entry) and "resumes_at" not in entry
    # Steam schedules the full release: tracking resumes two weeks before it.
    full = EA + DAY * 400
    source["early_access"] = access(full)
    reconcile_tracking_entry(entry, EA + DAY * 100)
    assert entry["resumes_at"] == timestamp(full - DAY * 14)
    reconcile_tracking_entry(entry, full - DAY * 14)
    assert (entry["status"], source["status_reason"]) == ("active", "steam_full_release_pending")
    reconcile_tracking_entry(entry, full + DAY)
    assert source["status_reason"] == "steam_recent_release"
    assert entry["expires_at"] == timestamp(full + DAY * 30)
    reconcile_tracking_entry(entry, full + DAY * 30)
    assert (entry["status"], entry["status_reason"]) == ("expired", "release_window_elapsed")


def test_full_release_years_later_enrolls_a_game_never_tracked_in_early_access():
    full = NOW + DAY * 5
    state = normalize_tracking_state(None, NOW)
    entry = enroll_steam_mapping(state, steam_mapping(access(full), release=NOW - DAY * 700), NOW)
    assert entry and entry["tracking_sources"]["steam:100"]["status_reason"] == "steam_full_release_pending"
    assert enroll_steam_mapping(normalize_tracking_state(None, NOW), steam_mapping(access(), release=NOW - DAY * 700),
                                NOW) is None


def test_catalog_early_access_reaches_the_steam_source():
    row = {"appid": 100, "name": "Early game", "followers": 10, "release_start": "2026-07-30",
           "release_end": "2026-07-30", "release_precision": "day", "release_date_timezone": "Asia/Taipei"}
    payload = {"version": 2, "generated_at": timestamp(NOW), "count": 1, "games": [row]}
    facts = {"games": {"100": {"early_access": access(), "price": None}, "200": {"early_access": "bad"}}}
    attach_early_access(payload, facts)
    catalog = normalize_steam_catalog(payload, NOW)
    assert catalog[0]["early_access"] == access()
    payload["games"][0]["early_access"] = {"active": "yes"}
    assert "early_access" not in normalize_steam_catalog(payload, NOW)[0]
    state, entry = steam_entry(EA + DAY, access())
    reconcile_steam_catalog(state, normalize_steam_catalog(attach_early_access(
        {**payload, "games": [dict(row, release_start="2026-07-30")]},
        {"games": {"100": {"early_access": access(EA + DAY * 400)}}}), NOW),
        {"games": {"100": {**steam_mapping(), "checked_at": timestamp(NOW)}}}, NOW)
    assert entry["tracking_sources"]["steam:100"]["early_access"]["full_release_at"] == timestamp(EA + DAY * 400)


def observation(at, viewers=8000, release=None, precision=None, verification="new", **launches):
    row = {"game_id": "1", "game_name": "Early game", "viewer_count": viewers,
           "verification": {"status": verification, "observed_at": timestamp(at),
                            "expires_at": timestamp(at + DAY)} if verification else {"status": "pending"}}
    if release is not None:
        row["release_evidence"] = {"first_release_date": timestamp(release), "checked_at": timestamp(at)}
        if precision:
            row["release_evidence"].update(release_precision=precision, early_access_at=None,
                                           full_release_at=None, **launches)
    return row


def test_a_demo_before_a_known_launch_pauses_until_the_launch_window():
    launch = NOW + DAY * 90
    state = normalize_tracking_state(None, NOW)
    entry = enroll_observation(state, observation(NOW, release=launch, precision="day"), NOW)
    assert (entry["status"], entry["status_reason"]) == ("active", "twitch_viewers")
    assert entry["expires_at"] == timestamp(NOW + DAY * 30)
    reconcile_tracking_entry(entry, NOW + DAY * 31)
    assert (entry["status"], entry["status_reason"]) == ("expired", "awaiting_launch")
    assert entry["resumes_at"] == timestamp(launch - DAY * 14)
    reconcile_tracking_entry(entry, launch - DAY * 14)
    assert (entry["status"], entry["status_reason"]) == ("active", "within_release_window")
    assert entry["expires_at"] == timestamp(launch + DAY * 30)


def test_a_placeholder_date_is_no_launch_and_the_game_waits():
    placeholder = datetime(2027, 12, 31, tzinfo=timezone.utc)
    state = normalize_tracking_state(None, NOW)
    entry = enroll_observation(state, observation(NOW, release=placeholder, precision="year"), NOW)
    assert entry["expires_at"] == timestamp(NOW + DAY * 30)
    reconcile_tracking_entry(entry, NOW + DAY * 31)
    assert (entry["status"], entry["status_reason"]) == ("expired", "awaiting_launch")
    assert "resumes_at" not in entry


def test_threshold_brings_back_a_waiting_game_but_cannot_stretch_a_launch():
    state, entry = steam_entry(EA + DAY, access())
    reconcile_tracking_entry(entry, EA + DAY * 45)
    assert awaiting_launch(entry)
    later = EA + DAY * 45
    # Not new by any evidence, and under the threshold: still waiting.
    assert enroll_observation(state, observation(later, viewers=6999, verification="not_new"), later,
                              revival=True) is entry
    assert entry["status"] == "expired"
    enroll_observation(state, observation(later, verification="not_new"), later, revival=True)
    assert (entry["status"], entry["status_reason"]) == ("active", "twitch_viewers")
    assert entry["expires_at"] == timestamp(later + DAY * 30)
    assert entry["tracking_sources"]["twitch_new"]["enrollment"]["source"] == "awaiting_launch"
    # A normal launch: a late hit inside its window does not extend it.
    launch = NOW - DAY * 5
    fresh = normalize_tracking_state(None, NOW)
    normal = enroll_observation(fresh, observation(NOW, release=launch, precision="day"), NOW)
    late = launch + DAY * 29
    enroll_observation(fresh, observation(late, release=launch, precision="day"), late)
    assert normal["expires_at"] == timestamp(launch + DAY * 30)
    assert "hit_until" not in normal["tracking_sources"]["twitch_new"]


def test_collection_measures_a_waiting_game_and_revives_it_at_the_threshold(tmp_path, monkeypatch):
    state, entry = steam_entry(EA + DAY, access())
    entry["igdb_id"] = "101"
    state["updated_at"] = timestamp(NOW - DAY)
    client = FakeClient([("games/top", page([game(1, "101"), game(2, "102")])),
                         ("streams", page([stream(1, 11, 9000)])), ("streams", page([stream(2, 22, 100)]))])
    stub_hints(monkeypatch, {"101": timestamp(EA), "102": timestamp(NOW - DAY * 300)})
    from collectors.twitch_candidates import collect_candidates
    result = collect_candidates(client_id="test", client_secret="test", client=client, now=NOW,
                                registry_path=registry(tmp_path), tracking_state=state)
    revived = result["tracking_state"]["games"]["1"]
    assert (revived["status"], revived["status_reason"]) == ("active", "active_tracking_sources") or \
        (revived["status"], revived["status_reason"]) == ("active", "twitch_viewers")
    assert [row["game_id"] for row in result["tracked_games"]] == ["1"]
    assert result["candidate_games"] == []
    assert result["coverage"]["revived_game_ids"] == ["1"]
    assert result["coverage"]["awaiting_launch_count"] == 0


def test_collection_checks_igdb_for_waiting_games_outside_the_directory(tmp_path, monkeypatch):
    launch = NOW + DAY * 3
    state = normalize_tracking_state(None, NOW - DAY * 40)
    entry = enroll_observation(state, observation(NOW - DAY * 40), NOW - DAY * 40)
    entry["igdb_id"] = "101"
    reconcile_tracking_entry(entry, NOW - DAY)
    assert awaiting_launch(entry)
    batches = stub_hints(monkeypatch, {"101": timestamp(launch)})
    client = FakeClient([("games/top", page([])), ("games", page([game(1, "101")])), ("streams", page([]))])
    from collectors.twitch_candidates import collect_candidates
    result = collect_candidates(client_id="test", client_secret="test", client=client, now=NOW,
                                registry_path=registry(tmp_path), tracking_state=state)
    assert ["101"] in batches
    tracked = result["tracking_state"]["games"]["1"]
    # The hint stub knows no precision: the date is taken as exact, as before.
    assert (tracked["status"], tracked["status_reason"]) == ("active", "within_release_window")
    assert [row["game_id"] for row in result["tracked_games"]] == ["1"]
