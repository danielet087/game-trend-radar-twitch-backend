from datetime import datetime, timezone
import json

from scripts.recover_twitch_tracking import recover_tracking


def test_recovery_replays_thirty_day_admission_preserves_metrics_and_filters_expired_non_games(tmp_path):
    root = tmp_path / "data"
    (root / "twitch_history").mkdir(parents=True)
    observed = "2026-09-29T07:00:00Z"
    def row(game_id, release, viewers=8000):
        return {"game_id": game_id, "game_name": "Game " + game_id, "viewer_count": viewers,
                "streamer_count": 4, "median_viewer_count": 40,
                "measurement_started_at": observed, "measurement_finished_at": observed,
                "verification": {"status": "pending"},
                "release_experiment": {"igdb_first_release_date": {
                    "status": "evaluated", "predicted_new": False, "window_days": 14,
                    "release_at": release, "metadata_observed_at": observed, "evaluated_at": observed}}}
    rows = [row("1", "2026-09-09T00:00:00Z"), row("2", "2026-08-31T12:00:00Z"),
            row("509659", "2026-09-20T00:00:00Z"), row("26936", "2026-09-20T00:00:00Z"),
            row("3", "2026-09-20T00:00:00Z", viewers=6999)]
    unknown = row("4", "2026-09-20T00:00:00Z")
    unknown["release_experiment"] = {}
    rows.append(unknown)
    history = {"schema_version": 1, "date": "2026-09-29", "timezone": "Asia/Taipei",
               "hours": {observed: {"generated_at": observed, "min_viewers": 7000, "games": rows}}}
    (root / "twitch_history/2026-09-29.json").write_text(json.dumps(history))
    latest = {"schema_version": 2, "generated_at": "2026-09-30T07:00:00Z",
              "min_viewers": 7000, "coverage": {"collection_complete": True}, "candidate_games": []}
    (root / "twitch_live.json").write_text(json.dumps(latest))
    before = {p: p.read_bytes() for p in root.rglob("*.json")}
    state = recover_tracking(tmp_path, datetime(2026, 9, 30, 13, tzinfo=timezone.utc), source_commit="fixture")
    assert set(state["games"]) == {"1", "2"}
    active = state["games"]["1"]
    assert active["status"] == "active" and state["games"]["2"]["status"] == "expired"
    assert active["first_seen_at"] == active["last_seen_at"] == observed
    assert active["enrollment"]["source"] == "igdb_first_release_date"
    assert active["enrollment"]["kind"] == "history_recovery"
    assert active["last_observation"]["viewer_count"] == 8000
    assert active["last_observation"]["release_experiment"] == rows[0]["release_experiment"]
    assert before == {p: p.read_bytes() for p in root.rglob("*.json")}
