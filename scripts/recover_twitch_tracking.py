"""Recover persistent enrollments from saved measurements, without backfilling metrics."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path

from radar_backend.domain.twitch_candidates import NON_GAME_IDS
from radar_backend.state.json_snapshot import write_json
from radar_backend.domain.twitch_newness import parse_timestamp, timestamp
from radar_backend.adapters.twitch_tracking import (
    enroll_observation, normalize_tracking_state, reconcile_tracking_entry, release_from_observation,
)


def recover_tracking(frontend: Path, now: datetime, *, source_commit: str | None = None) -> dict:
    observations = []
    history_files = sorted((frontend / "data/twitch_history").glob("*.json"))
    for path in history_files:
        history = json.loads(path.read_text(encoding="utf-8"))
        if history.get("schema_version") != 1 or not isinstance(history.get("hours"), dict):
            raise ValueError(f"Invalid historical snapshot: {path.name}")
        for hour in history["hours"].values():
            at = parse_timestamp(hour["generated_at"])
            if not isinstance(hour.get("games"), list):
                raise ValueError(f"Invalid historical games: {path.name}")
            if at <= now:
                observations.extend((at, deepcopy(row), hour.get("min_viewers", 7000)) for row in hour["games"])

    latest = json.loads((frontend / "data/twitch_live.json").read_text(encoding="utf-8"))
    if latest.get("schema_version") != 2 or latest.get("coverage", {}).get("collection_complete") is not True:
        raise ValueError("Recovery requires a complete schema-v2 latest snapshot")
    latest_at = parse_timestamp(latest["generated_at"])
    if latest_at > now:
        raise ValueError("Latest snapshot is in the future")
    for row in latest["candidate_games"] + latest.get("tracked_games", []):
        observations.append((latest_at, deepcopy(row), latest.get("min_viewers", 7000)))

    state = normalize_tracking_state(None, now)
    for at, row, threshold in sorted(observations, key=lambda item: item[0]):
        entry = enroll_observation(state, row, at, min_viewers=threshold, non_game_ids=NON_GAME_IDS)
        if entry:
            entry["enrollment"]["kind"] = "history_recovery"
    # The newest exclusion/date evidence may expire an enrollment, but does
    # not manufacture a new audience observation or overwrite its timestamp.
    for row in latest.get("excluded_games", []):
        entry = state["games"].get(str(row.get("game_id")))
        if entry:
            released, source = release_from_observation(row)
            reconcile_tracking_entry(entry, now, release_at=released, release_source=source,
                                     non_game_ids=NON_GAME_IDS)
            for key in ("igdb_id", "box_art_url"):
                if row.get(key):
                    entry[key] = row[key]
    state = normalize_tracking_state(state, now, non_game_ids=NON_GAME_IDS)
    state["updated_at"] = timestamp(now)
    state["recovery"] = {
        "source_repository": "danielet087/game-trend-radar", "source_commit": source_commit,
        "history_files": [path.name for path in history_files], "recovered_at": timestamp(now),
        "rule": "historical_admission_replayed_with_igdb_30_days",
        "note": "Original observations and hourly history remain unchanged; no missing metrics are reconstructed.",
    }
    return state


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("frontend", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--at", help="Timezone-aware recovery time; defaults to actual UTC now")
    parser.add_argument("--source-commit")
    args = parser.parse_args()
    now = parse_timestamp(args.at) if args.at else datetime.now(timezone.utc)
    state = recover_tracking(args.frontend, now, source_commit=args.source_commit)
    write_json(state, args.output)
    print(json.dumps({"active": sum(row["status"] == "active" for row in state["games"].values()),
                      "total": len(state["games"]), "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
