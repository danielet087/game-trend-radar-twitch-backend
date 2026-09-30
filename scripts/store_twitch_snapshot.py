"""Merge one complete census into the newest frontend and a Taipei daily history."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path

from collectors.twitch_candidates import parse_timestamp, timestamp
from collectors.twitch_live import write_json
from collectors.twitch_tracking import normalize_tracking_state
from scripts.load_twitch_tracking import validate_persisted_tracking

TAIPEI = timezone(timedelta(hours=8))
STATUS_PATH = "data/twitch_collection_status.json"
TRACKING_PATH = "data/twitch_tracking.json"


def merge_tracking_state(existing: dict | None, incoming: dict) -> dict:
    """Union enrollments when publishing against a newer frontend checkout.

    A late result may contribute a previously unknown enrollment, but cannot
    rewind an entry's metadata, status or most recent real observation. A
    terminal status is not deleted: it remains a tombstone for later readers.
    """
    new = validate_persisted_tracking(incoming)
    if existing is None:
        return new
    old = validate_persisted_tracking(existing)
    new_time, old_time = (parse_timestamp(state["updated_at"]) for state in (new, old))
    result = deepcopy(new if new_time >= old_time else old)
    result["games"] = {}
    for game_id in sorted(set(old["games"]) | set(new["games"])):
        previous, current = old["games"].get(game_id), new["games"].get(game_id)
        if previous is None or current is None:
            result["games"][game_id] = deepcopy(current if previous is None else previous)
            continue
        old_updated = parse_timestamp(previous.get("updated_at", old["updated_at"]))
        new_updated = parse_timestamp(current.get("updated_at", new["updated_at"]))
        merged = deepcopy(current if new_updated >= old_updated else previous)
        # Enrollment is historical evidence and may have been recovered while
        # this job was running. Keep the earliest evidence rather than resetting
        # first_seen_at to the next time that viewers cross the discovery bar.
        for field in ("first_seen_at", "enrolled_at"):
            values = [entry[field] for entry in (previous, current) if entry.get(field)]
            if values:
                merged[field] = min(values, key=parse_timestamp)
        enrollments = [entry["enrollment"] for entry in (previous, current)
                       if isinstance(entry.get("enrollment"), dict) and entry["enrollment"].get("observed_at")]
        if enrollments:
            merged["enrollment"] = deepcopy(min(enrollments, key=lambda item: parse_timestamp(item["observed_at"])))
        observations = [entry["last_observation"] for entry in (previous, current)
                        if isinstance(entry.get("last_observation"), dict)]
        if observations:
            merged["last_observation"] = deepcopy(max(observations,
                key=lambda item: parse_timestamp(item["observation_at"])))
        seen_at = [entry["last_seen_at"] for entry in (previous, current) if entry.get("last_seen_at")]
        if seen_at:
            merged["last_seen_at"] = max(seen_at, key=parse_timestamp)
        result["games"][game_id] = merged
    # Reconcile expiry at the newest state time without deleting retained rows.
    return normalize_tracking_state(result, now=max(new_time, old_time))


def observed_rows(payload: dict) -> list[dict]:
    """Deduplicate discovery and enrolled games; only real observations belong in history."""
    rows = {row["game_id"]: row for row in payload["candidate_games"]}
    for row in payload.get("tracked_games", []):
        previous = rows.get(row["game_id"])
        if previous is not None and any(previous.get(field) != row.get(field) for field in (
            "viewer_count", "streamer_count", "median_viewer_count",
            "measurement_started_at", "measurement_finished_at", "filtered_audience",
        )):
            raise ValueError("Candidate and tracked observations disagree")
        rows[row["game_id"]] = row
    return list(rows.values())


def validate_schedule(payload: dict) -> dict | None:
    """New collectors use real observation time; old published hours stay intact."""
    if "collection_schedule" not in payload:
        return None
    schedule = payload["collection_schedule"]
    if not isinstance(schedule, dict):
        raise ValueError("Invalid collection schedule metadata")
    target = parse_timestamp(schedule.get("target_slot"))
    if schedule["target_slot"] != timestamp(target.replace(minute=0, second=0, microsecond=0)):
        raise ValueError("Target slot must be a canonical UTC hour")
    if schedule.get("trigger_source") not in {"cloudflare", "schedule", "manual"}:
        raise ValueError("Invalid collection trigger source")
    for key in ("run_id", "run_attempt"):
        value = schedule.get(key)
        if not isinstance(value, str) or not value.isascii() or not value.isdigit() or int(value) < 1:
            raise ValueError(f"Invalid collection {key}")
    started = parse_timestamp(payload.get("collection_started_at"))
    completed = parse_timestamp(payload.get("generated_at"))
    if not target <= started <= completed:
        raise ValueError("Target, collection start and completion are out of order")
    return schedule


def observation_order(payload: dict) -> tuple[datetime, datetime]:
    """A late-published older sample must not replace a newer observation."""
    completed = parse_timestamp(payload["generated_at"])
    observed = parse_timestamp(payload["collection_started_at"]) if "collection_schedule" in payload else completed
    return observed, completed


def validate_receipt(receipt: dict) -> tuple[datetime, datetime]:
    if not isinstance(receipt, dict) or receipt.get("schema_version") != 1 or receipt.get("collection_complete") is not True:
        raise ValueError("Existing collection status is malformed; refusing to overwrite it")
    started, completed = (parse_timestamp(receipt.get(key)) for key in ("collection_started_at", "completed_at"))
    observed_slot = timestamp(started.replace(minute=0, second=0, microsecond=0))
    expected_history = f"data/twitch_history/{started.astimezone(TAIPEI).date().isoformat()}.json"
    if (started > completed or receipt.get("observed_slot") != observed_slot
            or receipt.get("generated_at") != timestamp(completed)
            or receipt.get("history_path") != expected_history):
        raise ValueError("Existing collection status has inconsistent observation times")
    target = parse_timestamp(receipt.get("target_slot"))
    if receipt["target_slot"] != timestamp(target.replace(minute=0, second=0, microsecond=0)) or target > started:
        raise ValueError("Existing collection status has an invalid target slot")
    return started, completed


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
    schedule = validate_schedule(payload)
    if type(payload.get("min_viewers")) is not int or payload["min_viewers"] < 1:
        raise ValueError("Invalid threshold")
    if not isinstance(payload.get("candidate_games"), list):
        raise ValueError("Missing candidate list")
    tracked = payload.get("tracked_games", [])
    if not isinstance(tracked, list):
        raise ValueError("Invalid tracked game list")
    tracking = validate_persisted_tracking(payload["tracking_state"]) if "tracking_state" in payload else None
    if tracked and tracking is None:
        raise ValueError("Tracked observations require their persistent registry")
    for row in tracked:
        entry = tracking["games"].get(row.get("game_id"), {})
        if entry.get("status") != "active" or row.get("observation_status") != "current":
            raise ValueError("Only active, freshly observed tracked games may be published")
    candidate_ids = {row.get("game_id") for row in payload["candidate_games"]}
    for group in (payload["candidate_games"], tracked):
        ids = [row.get("game_id") for row in group]
        if len(set(ids)) != len(ids):
            raise ValueError("Invalid or duplicate game ID")
    seen = set()
    for row in observed_rows(payload):
        game_id = row["game_id"]
        if not isinstance(game_id, str) or not game_id.isdigit() or game_id in seen:
            raise ValueError("Invalid or duplicate game ID")
        seen.add(game_id)
        allowed_statuses = {"new", "pending"} if game_id in candidate_ids else {"new", "pending", "not_new"}
        if row.get("pagination_complete") is not True or row.get("verification", {}).get("status") not in allowed_statuses:
            raise ValueError("Unfinished or excluded category in candidate list")
        viewers, streamers, middle = row.get("viewer_count"), row.get("streamer_count"), row.get("median_viewer_count")
        minimum = payload["min_viewers"] if game_id in candidate_ids else 0
        if type(viewers) is not int or viewers < minimum or type(streamers) is not int or streamers < 0:
            raise ValueError("Invalid candidate metrics")
        if streamers == 0:
            if game_id in candidate_ids or viewers != 0 or middle is not None:
                raise ValueError("An empty stream census must have zero viewers and no median")
        elif type(middle) not in (float, int) or not math.isfinite(middle) or not 0 <= middle <= viewers:
            raise ValueError("Invalid median")
        if "filtered_audience" in row:
            validate_filtered_audience(row["filtered_audience"], viewers=viewers, streamers=streamers)
        if schedule is not None:
            started, completed = (parse_timestamp(payload[key]) for key in ("collection_started_at", "generated_at"))
            measured_start, measured_finish = (parse_timestamp(row.get(key)) for key in ("measurement_started_at", "measurement_finished_at"))
            if not started <= measured_start <= measured_finish <= completed:
                raise ValueError("Category measurement is outside the collection window")


def store_snapshot(payload: dict, frontend: Path) -> str:
    validate_snapshot(payload)
    scheduled = "collection_schedule" in payload
    observed = parse_timestamp(payload["collection_started_at"] if scheduled else payload["generated_at"])
    completed = parse_timestamp(payload["generated_at"])
    day = observed.astimezone(TAIPEI).date().isoformat()
    hour = timestamp(observed.replace(minute=0, second=0, microsecond=0))
    relative = f"data/twitch_history/{day}.json"
    history_path = frontend / relative
    history = json.loads(history_path.read_text(encoding="utf-8")) if history_path.exists() else {
        "schema_version": 1, "date": day, "timezone": "Asia/Taipei", "hours": {},
    }
    if history.get("date") != day or history.get("schema_version") != 1 or not isinstance(history.get("hours"), dict):
        raise ValueError("Existing history is malformed; refusing to overwrite it")
    latest_path = frontend / "data/twitch_live.json"
    latest = json.loads(latest_path.read_text(encoding="utf-8")) if latest_path.exists() else None
    status_path = frontend / STATUS_PATH
    status = json.loads(status_path.read_text(encoding="utf-8")) if scheduled and status_path.exists() else None
    status_order = validate_receipt(status) if status is not None else None
    tracking_path = frontend / TRACKING_PATH
    tracking = json.loads(tracking_path.read_text(encoding="utf-8")) if tracking_path.exists() else None
    if tracking_path.exists():
        validate_persisted_tracking(tracking)
    merged_tracking = (merge_tracking_state(tracking, payload["tracking_state"])
                       if "tracking_state" in payload else tracking)
    order = observation_order(payload)
    update_latest = latest is None or order > observation_order(latest)
    # Receipt and latest must describe the same result. A stale publication may
    # still fill its genuine historical slot, but cannot rewind current status.
    if status_order is not None and order < status_order:
        update_latest = False
    previous = history["hours"].get(hour)
    if not previous or order > observation_order(previous):
        history["hours"][hour] = {
            "generated_at": payload["generated_at"],
            "collection_started_at": payload["collection_started_at"],
            "min_viewers": payload["min_viewers"],
            "stop_reason": payload["coverage"]["stop_reason"],
            **({"collection_schedule": dict(payload["collection_schedule"])} if scheduled else {}),
            "games": [
                {**{key: row[key] for key in (
                    "game_id", "game_name", "viewer_count", "streamer_count", "median_viewer_count",
                    "measurement_started_at", "measurement_finished_at", "verification",
                )}, **{key: row[key] for key in ("release_experiment", "filtered_audience", "tracking",
                                                "observation_status", "observation_at") if key in row}}
                for row in observed_rows(payload)
            ],
        }
        history["hours"] = dict(sorted(history["hours"].items()))
        write_json(history, history_path)
    if update_latest:
        latest_payload = deepcopy(payload)
        if merged_tracking is not None and "tracking_state" in payload:
            latest_payload["tracking_state"] = merged_tracking
        write_json(latest_payload, latest_path)
    if merged_tracking is not None and merged_tracking != tracking:
        write_json(merged_tracking, tracking_path)
    same_observation = (latest is not None and
                        {key: value for key, value in latest.items() if key != "tracking_state"} ==
                        {key: value for key, value in payload.items() if key != "tracking_state"})
    if scheduled and (update_latest or same_observation) and (status_order is None or order >= status_order):
        receipt = {
            "schema_version": 1,
            "observed_slot": hour,
            "target_slot": payload["collection_schedule"]["target_slot"],
            "collection_started_at": timestamp(observed),
            "completed_at": timestamp(completed),
            "generated_at": timestamp(completed),
            "collection_complete": True,
            "run_id": payload["collection_schedule"]["run_id"],
            "history_path": relative,
        }
        if receipt != status:
            write_json(receipt, status_path)
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
