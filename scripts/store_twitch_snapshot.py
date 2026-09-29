"""Merge one complete census into the newest frontend and a Taipei daily history."""
from __future__ import annotations

import argparse
from datetime import timedelta, timezone
import json
import math
from pathlib import Path

from collectors.twitch_candidates import parse_timestamp, timestamp
from collectors.twitch_live import write_json

TAIPEI = timezone(timedelta(hours=8))


def validate_filtered_audience(value: dict, *, viewers: int, streamers: int) -> None:
    """Keep the new population explicit; incomplete lookups cannot claim a median."""
    if not isinstance(value, dict) or value.get("rule") != "followers_gt_1000_viewers_gte_10_v1":
        raise ValueError("Invalid filtered audience rule")
    for key, expected in (("min_followers_exclusive", 1000), ("min_viewers_inclusive", 10),
                          ("followers_max_age_hours", 24)):
        if type(value.get(key)) is not int or value[key] != expected:
            raise ValueError("Invalid filtered audience threshold")
    partitions = ("eligible_streamer_count", "excluded_low_viewer_count",
                  "excluded_low_follower_count", "unknown_follower_count")
    if any(type(value.get(key)) is not int or value[key] < 0 for key in partitions):
        raise ValueError("Invalid filtered audience counts")
    if sum(value[key] for key in partitions) != streamers:
        raise ValueError("Filtered audience counts do not match the census")
    eligible, unknown = value["eligible_streamer_count"], value["unknown_follower_count"]
    total, middle = value.get("eligible_viewer_count"), value.get("median_viewer_count")
    if type(total) is not int or not eligible * 10 <= total <= viewers or (eligible == 0 and total != 0):
        raise ValueError("Invalid filtered audience viewer total")
    if value.get("status") != ("partial" if unknown else "complete"):
        raise ValueError("Filtered audience status does not match coverage")
    if unknown or eligible == 0:
        if middle is not None:
            raise ValueError("An incomplete or empty audience cannot claim a median")
    elif type(middle) not in (int, float) or not math.isfinite(middle) or not 10 <= middle <= total:
        raise ValueError("Invalid filtered audience median")


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
        if "filtered_audience" in row:
            validate_filtered_audience(row["filtered_audience"], viewers=viewers, streamers=streamers)


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
                )}, **{key: row[key] for key in ("release_experiment", "filtered_audience") if key in row}}
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
