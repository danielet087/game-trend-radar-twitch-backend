"""Persistent new-game enrollment, independent of the live directory ranking.

The viewer threshold is an admission rule. Once admitted, a game remains in
the observation set until its known release date plus 30 days. Neither a
missing badge nor missing metadata is evidence that observation should end.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any

from collectors.twitch_newness import parse_timestamp, timestamp

TRACKING_DAYS = 30
STATUSES = {"active", "expired", "excluded"}


def normalize_tracking_state(payload: dict | None, now: datetime | None = None,
                             *, non_game_ids=()) -> dict:
    """Validate without discarding entries; optionally reconcile time expiry."""
    if payload is None:
        return {"schema_version": 1, "updated_at": timestamp(now) if now else None, "games": {}}
    if not isinstance(payload, dict) or payload.get("schema_version") != 1 or not isinstance(payload.get("games"), dict):
        raise ValueError("Invalid Twitch tracking state")
    state = deepcopy(payload)
    if state.get("updated_at") is None and state["games"]:
        raise ValueError("Tracking state with games requires an update timestamp")
    if state.get("updated_at") is not None:
        parse_timestamp(state["updated_at"])
    for game_id, entry in state["games"].items():
        if not isinstance(game_id, str) or not game_id.isdigit() or not isinstance(entry, dict):
            raise ValueError("Invalid tracked game ID or entry")
        if entry.get("game_id") != game_id or entry.get("status") not in STATUSES:
            raise ValueError("Invalid tracked game identity or status")
        if not isinstance(entry.get("game_name"), str) or not entry["game_name"]:
            raise ValueError("Tracked game requires a name")
        parse_timestamp(entry.get("first_seen_at"))
        enrollment = entry.get("enrollment")
        if not isinstance(enrollment, dict) or not enrollment.get("source"):
            raise ValueError("Tracked game requires enrollment evidence")
        parse_timestamp(enrollment.get("observed_at"))
        for field in ("first_seen_at", "last_seen_at", "updated_at", "release_at", "expires_at"):
            if entry.get(field) is not None:
                parse_timestamp(entry[field])
        if entry.get("last_observation") is not None and not isinstance(entry["last_observation"], dict):
            raise ValueError("Invalid tracked game observation")
        if entry.get("last_observation") is not None:
            observation = entry["last_observation"]
            if str(observation.get("game_id")) != game_id:
                raise ValueError("Tracked observation identity does not match")
            parse_timestamp(observation.get("observation_at"))
        if now is not None:
            reconcile_tracking_entry(entry, now, non_game_ids=non_game_ids)
    return state


def release_from_observation(row: dict) -> tuple[str | None, str | None]:
    """Get the recorded release date, including historical date evidence.

    Metadata cache age controls newness inference, not the lifetime of a
    previously established release date. An API outage must not extend a
    known release date indefinitely.
    """
    evidence = row.get("release_evidence") or {}
    experiments = row.get("release_experiment") or {}
    options = [
        (evidence.get("first_release_date"), "igdb_first_release_date"),
        ((experiments.get("igdb_first_release_date") or {}).get("release_at"), "igdb_first_release_date"),
        ((experiments.get("twitch_original_release_date") or {}).get("release_at"), "twitch_original_release_date"),
    ]
    for value, source in options:
        if value:
            try:
                return timestamp(parse_timestamp(value)), source
            except (ValueError, TypeError):
                continue
    return None, None


def reconcile_tracking_entry(entry: dict, now: datetime, *, release_at: str | None = None,
                             release_source: str | None = None, non_game_ids=()) -> dict:
    if release_at and not (entry.get("release_at") and entry.get("release_source") == "igdb_first_release_date"
                           and release_source == "twitch_original_release_date"):
        entry["release_at"] = timestamp(parse_timestamp(release_at))
        entry["release_source"] = release_source
    released = entry.get("release_at")
    entry["expires_at"] = timestamp(parse_timestamp(released) + timedelta(days=TRACKING_DAYS)) if released else None
    if entry["game_id"] in non_game_ids:
        entry["status"] = "excluded"
        entry["status_reason"] = "non_game_category"
    elif entry.get("status") == "excluded":
        # Explicit exclusions must not be revived by routine metadata refresh.
        pass
    elif entry["expires_at"] and now >= parse_timestamp(entry["expires_at"]):
        entry["status"] = "expired"
        entry["status_reason"] = "release_window_elapsed"
    else:
        entry["status"] = "active"
        entry["status_reason"] = "awaiting_release_date" if not released else "within_release_window"
    return entry


def admission_evidence(row: dict, observed_at: datetime) -> str | None:
    """Replay source-labelled evidence, including the former 14-day snapshots."""
    verification = row.get("verification") or {}
    if verification.get("status") == "not_new":
        return None
    release_at, source = release_from_observation(row)
    if source == "igdb_first_release_date":
        # Re-evaluate historical IGDB evidence under the requested 30-day rule.
        trial = (row.get("release_experiment") or {}).get(source) or {}
        evidence = row.get("release_evidence") or {}
        metadata_at = evidence.get("checked_at") or trial.get("metadata_observed_at")
        try:
            fresh = metadata_at and parse_timestamp(metadata_at) <= observed_at < parse_timestamp(metadata_at) + timedelta(hours=24)
            if fresh and observed_at - parse_timestamp(release_at) < timedelta(days=TRACKING_DAYS):
                return source
        except (ValueError, TypeError):
            pass
    if verification.get("status") == "new":
        try:
            if parse_timestamp(verification["observed_at"]) <= observed_at < parse_timestamp(verification["expires_at"]):
                return "twitch_directory_dom"
        except (ValueError, TypeError, KeyError):
            pass
    trial = (row.get("release_experiment") or {}).get("twitch_original_release_date") or {}
    if trial.get("status") == "evaluated" and trial.get("predicted_new") is True:
        return "twitch_original_release_date"
    return None


def tracking_metadata(entry: dict) -> dict:
    return {key: deepcopy(entry.get(key)) for key in (
        "status", "status_reason", "first_seen_at", "last_seen_at", "release_at",
        "release_source", "expires_at", "enrollment",
    )}


def enroll_observation(state: dict, row: dict, observed_at: datetime, *,
                       min_viewers: int = 7000, non_game_ids=(),
                       eligibility_at: datetime | None = None) -> dict | None:
    """Upsert a successful observation; return its entry or None if unqualified.

    Historical backfills should replay chronologically, using each snapshot's
    own observed_at, then normalize the state at the current time once.
    """
    game_id = str(row.get("game_id") or "")
    if not game_id.isdigit() or game_id in non_game_ids:
        if game_id in state["games"]:
            reconcile_tracking_entry(state["games"][game_id], observed_at, non_game_ids=non_game_ids)
        return None
    entry = state["games"].get(game_id)
    eligible_time = eligibility_at or observed_at
    if entry is None:
        count = row.get("viewer_count")
        source = admission_evidence(row, eligible_time)
        if type(count) not in (int, float) or count < min_viewers or not source:
            return None
        entry = state["games"][game_id] = {
            "game_id": game_id, "game_name": row["game_name"],
            "first_seen_at": timestamp(observed_at), "status": "active",
            "enrollment": {"source": source, "observed_at": timestamp(observed_at),
                           "viewer_count": count, "min_viewers": min_viewers},
        }
    for key in ("game_name", "igdb_id", "box_art_url"):
        if row.get(key):
            entry[key] = row[key]
    released, source = release_from_observation(row)
    reconcile_tracking_entry(entry, eligible_time, release_at=released, release_source=source,
                             non_game_ids=non_game_ids)
    entry["last_seen_at"] = timestamp(observed_at)
    entry["updated_at"] = timestamp(observed_at)
    row["tracking"] = tracking_metadata(entry)
    row["observation_at"] = timestamp(observed_at)
    row["observation_status"] = "current"
    row["observation_freshness"] = "fresh"
    entry["last_observation"] = deepcopy(row)
    state["updated_at"] = timestamp(observed_at)
    return entry
