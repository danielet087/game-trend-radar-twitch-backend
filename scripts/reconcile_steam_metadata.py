"""Refresh published Steam identity metadata without collecting Twitch metrics.

Only local catalog, registry and confirmed identity cache files are read. This
command has no API client, credentials or lookup fallback, and never writes an
hourly history, collection receipt, discovery cache or Steam catalog.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path

from radar_backend.adapters.steam_twitch_mapping import normalize_steam_catalog, refresh_mappings
from radar_backend.domain.twitch_candidates import NON_GAME_IDS
from radar_backend.domain.twitch_newness import parse_timestamp, timestamp
from radar_backend.adapters.twitch_tracking import normalize_tracking_state, reconcile_steam_catalog, tracking_metadata
from radar_backend.state.validation import (
    validate_persisted_discovery, validate_persisted_mapping, validate_persisted_tracking,
)
from radar_backend.adapters.twitch_snapshot import validate_snapshot

ROW_LISTS = ("candidate_games", "tracked_games", "top_games", "excluded_games")
ROW_METADATA = {"steam_matches", "tracking"}
SNAPSHOT_METADATA = {"tracking_state", "steam_mapping_state", "steam_catalog_summary"}


def _row_measurement(row: dict) -> dict:
    return {key: deepcopy(value) for key, value in row.items() if key not in ROW_METADATA}


def measurement_projection(snapshot: dict) -> dict:
    """Keep every census value and its provenance, including unknown fields."""
    projected = {key: deepcopy(value) for key, value in snapshot.items() if key not in SNAPSHOT_METADATA}
    for key in ROW_LISTS:
        if key in projected:
            projected[key] = [_row_measurement(row) for row in projected[key]]
    return projected


def _validate_saved_census(snapshot: dict) -> None:
    # Metadata can expire a membership after its last real measurement. The
    # census publisher's active-membership guard applies when taking a new
    # observation; an offline refresh must also accept its own retained output.
    validation = deepcopy(snapshot)
    tracked = validation.get("tracked_games", [])
    if isinstance(tracked, list) and tracked:
        registry = validate_persisted_tracking(validation.get("tracking_state"))
        registry = normalize_tracking_state(registry, parse_timestamp(validation["generated_at"]),
                                            non_game_ids=NON_GAME_IDS)
        for row in tracked:
            if not isinstance(row, dict) or row.get("game_id") not in registry["games"]:
                raise ValueError("Saved tracked observation requires its registry identity")
            registry["games"][row["game_id"]]["status"] = "active"
        validation["tracking_state"] = registry
    validate_snapshot(validation)


def reconcile_metadata(snapshot: dict, tracking: dict, mapping: dict, discovery: dict,
                       catalog: dict, now: datetime) -> tuple[dict, dict, dict, dict]:
    """Build and validate all output documents before permitting any file write."""
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("Metadata clock must include a timezone")
    clock = now.astimezone(timezone.utc)
    if not isinstance(snapshot, dict):
        raise ValueError("Latest Twitch snapshot must be an object")
    _validate_saved_census(snapshot)
    previous_tracking = validate_persisted_tracking(tracking)
    previous_mapping = validate_persisted_mapping(mapping)
    discoveries = validate_persisted_discovery(discovery)
    normalized_catalog = normalize_steam_catalog(catalog, clock)
    clocks = [snapshot["generated_at"], previous_tracking["updated_at"], catalog["generated_at"]]
    clocks.extend(state["updated_at"] for state in (previous_mapping, discoveries) if state.get("updated_at"))
    if any(parse_timestamp(value) > clock for value in clocks):
        raise ValueError("Metadata clock cannot precede an input document")
    for key in ROW_LISTS:
        rows = snapshot.get(key, [])
        if (not isinstance(rows, list) or any(not isinstance(row, dict) or
                not isinstance(row.get("game_id"), str) or not row["game_id"].isascii() or
                not row["game_id"].isdigit() or int(row["game_id"]) <= 0 for row in rows)):
            raise ValueError(f"Invalid {key} observations")

    mappings = refresh_mappings(None, catalog, previous_mapping, clock,
                               discovery_state=discoveries, tracking_state=previous_tracking,
                               allow_lookup=False)
    mappings = validate_persisted_mapping(mappings)
    if mappings.get("report", {}).get("lookup_count", 0) != 0:
        raise ValueError("Metadata reconciliation cannot perform identity lookups")
    registry = deepcopy(previous_tracking)
    reconcile_steam_catalog(registry, normalized_catalog, mappings, clock, non_game_ids=NON_GAME_IDS)

    # Stored Twitch names identify the original measured category. Localized
    # Steam display names belong only in Steam metadata, never census fields.
    for game_id, previous in previous_tracking["games"].items():
        registry["games"][game_id]["game_name"] = previous["game_name"]

    by_appid = {steam["steam_appid"]: steam for steam in normalized_catalog}
    matches_by_game = {}
    for appid, entry in mappings["games"].items():
        if entry.get("status") == "matched" and appid in by_appid:
            matches_by_game.setdefault(entry["twitch_game_id"], []).append(deepcopy(by_appid[appid]))

    def enrich(row: dict) -> None:
        game_id = row["game_id"]
        matches = matches_by_game.get(game_id, [])
        if matches or "steam_matches" in row:
            row["steam_matches"] = deepcopy(matches)
        if game_id in registry["games"]:
            row["tracking"] = tracking_metadata(registry["games"][game_id])

    latest = deepcopy(snapshot)
    for key in ROW_LISTS:
        for row in latest.get(key, []):
            enrich(row)
    for game_id, entry in registry["games"].items():
        observation = entry.get("last_observation")
        if observation is not None:
            before = _row_measurement(observation)
            enrich(observation)
            if _row_measurement(observation) != before:
                raise ValueError("Registry observation changed during metadata reconciliation")

    registry = validate_persisted_tracking(registry)
    latest["tracking_state"] = deepcopy(registry)
    latest["steam_mapping_state"] = deepcopy(mappings)
    latest["steam_catalog_summary"] = {
        "status": "ok", "catalog_games": len(normalized_catalog),
        "recent_releases": sum(bool(row["is_recent"]) for row in normalized_catalog),
        "matched_games": sum(row.get("status") == "matched" for row in mappings["games"].values()),
        "mapping_report": deepcopy(mappings.get("report")),
    }
    if measurement_projection(latest) != measurement_projection(snapshot):
        raise ValueError("Latest census changed during metadata reconciliation")

    changed_appids = sorted((appid for appid, entry in mappings["games"].items()
                             if entry.get("status") == "matched" and any(
                                 entry.get(key) != previous_mapping["games"].get(appid, {}).get(key)
                                 for key in ("steam", "status", "igdb_id", "twitch_game_id"))), key=int)
    changed_games = set()
    for key in ROW_LISTS:
        for before, after in zip(snapshot.get(key, []), latest.get(key, [])):
            if any(before.get(field) != after.get(field) for field in ROW_METADATA):
                changed_games.add(after["game_id"])
    for game_id, entry in registry["games"].items():
        if entry.get("steam_matches") != previous_tracking["games"].get(game_id, {}).get("steam_matches"):
            changed_games.add(game_id)
    report = {"status": "metadata_reconciled", "metadata_updated_at": timestamp(clock),
              "observation_generated_at": snapshot["generated_at"], "lookup_count": 0,
              "enriched_steam_appids": changed_appids,
              "enriched_twitch_game_ids": sorted(changed_games, key=int),
              "new_registry_game_ids": sorted(set(registry["games"]) - set(previous_tracking["games"]), key=int),
              "observed_rows_unchanged": True}
    return latest, registry, mappings, report


def reconcile_frontend(frontend: Path, now: datetime, *, steam_catalog: Path | None = None,
                       dry_run: bool = False) -> dict:
    data = frontend / "data"
    paths = {"snapshot": data / "twitch_live.json", "tracking": data / "twitch_tracking.json",
             "mapping": data / "twitch_steam_mapping.json", "discovery": data / "twitch_steam_discovery.json",
             "catalog": steam_catalog or data / "steam_upcoming.json"}
    if paths["catalog"].resolve() in {paths[key].resolve() for key in ("snapshot", "tracking", "mapping")}:
        raise ValueError("Steam catalog cannot be an output document")
    originals = {key: path.read_bytes() for key, path in paths.items()}
    inputs = {key: json.loads(content) for key, content in originals.items()}
    latest, registry, mappings, report = reconcile_metadata(**inputs, now=now)
    documents = {"snapshot": latest, "tracking": registry, "mapping": mappings}
    # Pre-serialize every output and check the local inputs have not changed
    # during reconciliation. The caller must retry using a fresh local revision.
    rendered = {key: (json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
                for key, document in documents.items()}
    if any(path.read_bytes() != originals[key] for key, path in paths.items()):
        raise ValueError("Frontend inputs changed during metadata reconciliation")
    report["dry_run"] = dry_run
    report["output_files"] = [str(paths[key]) for key in documents]
    if not dry_run:
        for key, content in rendered.items():
            paths[key].write_bytes(content)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frontend-dir", type=Path, default=Path("frontend"))
    parser.add_argument("--steam-catalog", type=Path, help="Override the local curated Steam catalog input")
    parser.add_argument("--now", help="Timezone-aware metadata clock; defaults to actual UTC now")
    parser.add_argument("--dry-run", action="store_true", help="Validate and report without writing any files")
    args = parser.parse_args()
    clock = parse_timestamp(args.now) if args.now else datetime.now(timezone.utc)
    report = reconcile_frontend(args.frontend_dir, clock, steam_catalog=args.steam_catalog, dry_run=args.dry_run)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
