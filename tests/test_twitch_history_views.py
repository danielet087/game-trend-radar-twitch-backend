"""Compact and closing views must show exactly what the raw day files show."""
import json

import pytest

from radar_backend.state.twitch_history_views import (
    CLOSE_PATH, COMPACT_GAME_FIELDS, COMPACT_PATH, HISTORY_PATH, closing_hour, project_history_views,
)


def game(game_id="1", viewers=10000, **extra):
    return {"game_id": game_id, "game_name": "Example", "viewer_count": viewers, "streamer_count": 5,
            "median_viewer_count": 300, "measurement_started_at": "x", "measurement_finished_at": "x",
            "verification": {"status": "new"}, "tracking": {"status": "active", "notes": "large"},
            "steam_matches": [{"steam_appid": "100"}], "release_experiment": {"rule": "large"},
            "filtered_audience": {"status": "complete"}, "audience_regions": {"zh": {"viewer_count": 3}},
            **extra}


def hour(generated, *games, started=None):
    entry = {"generated_at": generated, "min_viewers": 7000, "stop_reason": "category_directory_exhausted",
             "games": list(games) or [game()]}
    if started:
        entry["collection_started_at"] = started
        entry["collection_schedule"] = {"target_slot": started[:13] + ":00:00Z", "trigger_source": "cloudflare",
                                        "run_id": "1", "run_attempt": "1"}
    return entry


def write_day(root, day, hours):
    path = root / HISTORY_PATH / f"{day}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": 1, "date": day, "timezone": "Asia/Taipei", "hours": hours}, indent=2))
    return f"{HISTORY_PATH}{day}.json"


def view(root, base, day):
    return json.loads((root / base / f"{day}.json").read_text())


def test_compact_view_keeps_every_hour_with_only_chart_fields(tmp_path):
    day_path = write_day(tmp_path, "2026-11-15", {
        "2026-11-14T16:00:00Z": hour("2026-11-14T16:20:00Z", game("1", observation_status="retained"),
                                     started="2026-11-14T16:05:00Z"),
        "2026-11-14T17:00:00Z": hour("2026-11-14T17:20:00Z", game("2", observation_freshness="stale")),
        "2026-11-14T18:00:00Z": {"generated_at": "2026-11-14T18:20:00Z", "games": "invalid"},
    })
    project_history_views(tmp_path, day_path)
    compact = view(tmp_path, COMPACT_PATH, "2026-11-15")
    assert compact["view"] == "compact" and compact["date"] == "2026-11-15"
    assert list(compact["hours"]) == ["2026-11-14T16:00:00Z", "2026-11-14T17:00:00Z"]
    first = compact["hours"]["2026-11-14T16:00:00Z"]
    assert set(first) == {"generated_at", "collection_started_at", "collection_schedule", "games"}
    assert first["games"] == [{key: game("1", observation_status="retained")[key]
                               for key in COMPACT_GAME_FIELDS if key in game("1", observation_status="retained")}]
    assert "tracking" not in json.dumps(compact)
    assert compact["hours"]["2026-11-14T17:00:00Z"]["games"][0]["observation_freshness"] == "stale"
    raw = (tmp_path / COMPACT_PATH / "2026-11-15.json").read_text()
    assert raw.endswith("}\n") and "\n" not in raw[:-1]


def test_close_view_matches_the_page_end_of_day_choice(tmp_path):
    # 23:00 Taipei finished after midnight, so the page replays 22:00.
    day_path = write_day(tmp_path, "2026-11-15", {
        "2026-11-15T13:00:00Z": hour("2026-11-15T13:20:00Z", game("1", viewers=8000)),
        "2026-11-15T14:00:00Z": hour("2026-11-15T14:25:00Z", game("1", viewers=9000), started="2026-11-15T14:05:00Z"),
        "2026-11-15T15:00:00Z": hour("2026-11-15T16:02:00Z", game("1", viewers=9500), started="2026-11-15T15:05:00Z"),
    })
    project_history_views(tmp_path, day_path)
    closing = view(tmp_path, CLOSE_PATH, "2026-11-15")
    assert closing["view"] == "close"
    assert list(closing["hours"]) == ["2026-11-15T14:00:00Z"]
    assert closing["hours"]["2026-11-15T14:00:00Z"]["games"][0]["tracking"] == game()["tracking"]
    assert closing_hour(closing)[1] == "2026-11-15T14:00:00Z"


def test_close_view_rejects_hours_outside_the_taipei_day(tmp_path):
    day_path = write_day(tmp_path, "2026-11-15", {
        # Started the previous Taipei day; the page never shows it in this file.
        "2026-11-14T15:00:00Z": hour("2026-11-14T15:20:00Z", started="2026-11-14T15:05:00Z"),
        "2026-11-14T17:30:00Z": hour("2026-11-14T17:40:00Z"),
    })
    project_history_views(tmp_path, day_path)
    assert not (tmp_path / CLOSE_PATH / "2026-11-15.json").exists()


def test_views_never_lose_hours_to_a_smaller_day_file(tmp_path):
    full = {
        "2026-11-15T13:00:00Z": hour("2026-11-15T13:20:00Z"),
        "2026-11-15T14:00:00Z": hour("2026-11-15T14:20:00Z", game("1", viewers=9000)),
    }
    day_path = write_day(tmp_path, "2026-11-15", full)
    project_history_views(tmp_path, day_path)
    write_day(tmp_path, "2026-11-15", {"2026-11-15T13:00:00Z": hour("2026-11-15T13:30:00Z", game("1", viewers=7777))})
    project_history_views(tmp_path, day_path)
    compact = view(tmp_path, COMPACT_PATH, "2026-11-15")
    assert set(compact["hours"]) == set(full)
    assert compact["hours"]["2026-11-15T13:00:00Z"]["games"][0]["viewer_count"] == 7777
    closing = view(tmp_path, CLOSE_PATH, "2026-11-15")
    assert closing["hours"]["2026-11-15T14:00:00Z"]["games"][0]["viewer_count"] == 9000
    # A newer census for the same closing hour replaces the held one.
    write_day(tmp_path, "2026-11-15", {**full, "2026-11-15T14:00:00Z": hour("2026-11-15T14:40:00Z", game("1", viewers=9100))})
    project_history_views(tmp_path, day_path)
    assert view(tmp_path, CLOSE_PATH, "2026-11-15")["hours"]["2026-11-15T14:00:00Z"]["games"][0]["viewer_count"] == 9100


def test_backfill_keeps_compact_window_and_closes_every_day(tmp_path):
    days = ["2026-11-10", "2026-11-12", "2026-11-13", "2026-11-14"]
    for day in days:
        write_day(tmp_path, day, {f"{day}T01:00:00Z": hour(f"{day}T01:10:00Z")})
    stale = tmp_path / COMPACT_PATH / "2026-11-11.json"
    stale.parent.mkdir(parents=True)
    stale.write_text('{"schema_version": 1}\n')
    project_history_views(tmp_path)
    assert sorted(path.stem for path in (tmp_path / CLOSE_PATH).glob("*.json")) == days
    assert sorted(path.stem for path in (tmp_path / COMPACT_PATH).glob("*.json")) == days[1:]
    snapshot = {path: path.read_bytes() for path in tmp_path.rglob("*.json")}
    project_history_views(tmp_path)
    assert snapshot == {path: path.read_bytes() for path in tmp_path.rglob("*.json")}
    # A new day moves the window and fills nothing older than it.
    day_path = write_day(tmp_path, "2026-11-15", {"2026-11-15T01:00:00Z": hour("2026-11-15T01:10:00Z")})
    project_history_views(tmp_path, day_path)
    assert sorted(path.stem for path in (tmp_path / COMPACT_PATH).glob("*.json")) == ["2026-11-13", "2026-11-14", "2026-11-15"]


def test_malformed_view_is_rebuilt_from_raw_and_bad_paths_are_rejected(tmp_path):
    day_path = write_day(tmp_path, "2026-11-15", {"2026-11-15T01:00:00Z": hour("2026-11-15T01:10:00Z")})
    for base in (COMPACT_PATH, CLOSE_PATH):
        path = tmp_path / base / "2026-11-15.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"schema_version": 1, "date": "2026-11-14", "timezone": "Asia/Taipei", "hours": {}}\n')
    project_history_views(tmp_path, day_path)
    assert list(view(tmp_path, COMPACT_PATH, "2026-11-15")["hours"]) == ["2026-11-15T01:00:00Z"]
    assert list(view(tmp_path, CLOSE_PATH, "2026-11-15")["hours"]) == ["2026-11-15T01:00:00Z"]
    with pytest.raises(ValueError):
        project_history_views(tmp_path, "data/twitch_series/2026-11-15.json")
    write_day(tmp_path, "2026-11-16", {})
    (tmp_path / HISTORY_PATH / "2026-11-16.json").write_text('{"schema_version": 2}')
    with pytest.raises(ValueError):
        project_history_views(tmp_path, f"{HISTORY_PATH}2026-11-16.json")
