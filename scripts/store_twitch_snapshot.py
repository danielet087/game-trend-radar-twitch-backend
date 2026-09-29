"""Merge one complete census into the newest frontend and a Taipei daily history."""
from __future__ import annotations

import argparse
from datetime import timedelta, timezone
import json
from pathlib import Path

from collectors.twitch_candidates import parse_timestamp, timestamp
from collectors.twitch_live import write_json

TAIPEI = timezone(timedelta(hours=8))


def validate_snapshot(payload: dict) -> None:
    if payload.get("schema_version") != 2 or payload.get("coverage", {}).get("collection_complete") is not True:
        raise ValueError("Only complete schema-v2 snapshots may be published")
    parse_timestamp(payload["generated_at"])
    if type(payload.get("min_viewers")) is not int or payload["min_viewers"] < 1:
        raise ValueError("Invalid threshold")
    if not isinstance(payload.get("candidate_games"), list):
        raise ValueError("Missing candidate list")
    seen = set()
    for row in payload["candidate_games"]:
        game_id = row["game_id"]
        if not isinstance(game_id, str) or not game_id.isdigit() or game_id in seen:
            raise ValueError("Invalid or duplicate game ID")
        seen.add(game_id)
        if row.get("pagination_complete") is not True or row.get("verification", {}).get("status") not in {"new", "pending"}:
            raise ValueError("Unfinished or excluded category in candidate list")
        viewers, streamers, middle = row.get("viewer_count"), row.get("streamer_count"), row.get("median_viewer_count")
        if type(viewers) is not int or viewers < payload["min_viewers"] or type(streamers) is not int or streamers < 1:
            raise ValueError("Invalid candidate metrics")
        if type(middle) not in (float, int) or not 0 <= middle <= viewers:
            raise ValueError("Invalid median")


def store_snapshot(payload: dict, frontend: Path) -> str:
    validate_snapshot(payload)
    observed = parse_timestamp(payload["generated_at"])
    day = observed.astimezone(TAIPEI).date().isoformat()
    hour = timestamp(observed.replace(minute=0, second=0, microsecond=0))
    relative = f"data/twitch_history/{day}.json"
    history_path = frontend / relative
    history = json.loads(history_path.read_text(encoding="utf-8")) if history_path.exists() else {
        "schema_version": 1, "date": day, "timezone": "Asia/Taipei", "hours": {},
    }
    if history.get("date") != day or history.get("schema_version") != 1 or not isinstance(history.get("hours"), dict):
        raise ValueError("Existing history is malformed; refusing to overwrite it")
    previous = history["hours"].get(hour)
    if not previous or observed > parse_timestamp(previous["generated_at"]):
        history["hours"][hour] = {
            "generated_at": payload["generated_at"],
            "collection_started_at": payload["collection_started_at"],
            "min_viewers": payload["min_viewers"],
            "stop_reason": payload["coverage"]["stop_reason"],
            "games": [
                {**{key: row[key] for key in (
                    "game_id", "game_name", "viewer_count", "streamer_count", "median_viewer_count",
                    "measurement_started_at", "measurement_finished_at", "verification",
                )}, **({"release_experiment": row["release_experiment"]} if "release_experiment" in row else {})}
                for row in payload["candidate_games"]
            ],
        }
        history["hours"] = dict(sorted(history["hours"].items()))
        write_json(history, history_path)
    latest_path = frontend / "data/twitch_live.json"
    latest = json.loads(latest_path.read_text(encoding="utf-8")) if latest_path.exists() else None
    if latest is None or observed > parse_timestamp(latest["generated_at"]):
        write_json(payload, latest_path)
    return relative


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("snapshot")
    parser.add_argument("frontend")
    args = parser.parse_args()
    payload = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
    print(store_snapshot(payload, Path(args.frontend)))


if __name__ == "__main__":
    main()
