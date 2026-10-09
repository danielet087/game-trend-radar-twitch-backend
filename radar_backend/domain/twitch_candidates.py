"""Pure candidate observations, census validation and release metadata rules."""

from __future__ import annotations
from datetime import datetime
from typing import Any


class IncompleteCollection(RuntimeError):
    """A partial enumeration must not replace the last usable snapshot."""


NON_GAME_IDS = {
    "509658": "Just Chatting",
    "509672": "IRL",
    "509663": "Special Events",
    "509659": "ASMR",
    "26936": "Music",
}


def validate_verifications(
    payload, *, parse_timestamp, timedelta_type
) -> dict[str, dict[str, Any]]:
    if payload.get("schema_version") != 1 or not isinstance(payload.get("observations"), dict):
        raise ValueError("Invalid verification registry")
    observations = payload["observations"]
    for game_id, row in observations.items():
        if not game_id.isdigit() or not isinstance(row, dict):
            raise ValueError("Invalid verification game ID or record")
        if row.get("status") not in {"new", "not_new"}:
            raise ValueError("Verification status must be explicit")
        if row.get("source") != "twitch_directory_dom" or not row.get("source_url"):
            raise ValueError("Verification needs a direct Twitch directory observation")
        observed = parse_timestamp(row["observed_at"])
        expires = parse_timestamp(row["expires_at"])
        if not observed < expires <= observed + timedelta_type(hours=24):
            raise ValueError("Badge observations may be cached for at most 24 hours")
    return observations


def verification_for(game_id: str, observations: dict, now: datetime, *, parse_timestamp) -> dict:
    row = observations.get(game_id)
    if row is None:
        return {"status": "pending", "reason": "no_observation"}
    observed, expires = (parse_timestamp(row["observed_at"]), parse_timestamp(row["expires_at"]))
    if observed <= now < expires:
        return dict(row)
    return {
        "status": "pending",
        "reason": "observation_expired_or_future",
        "previous_status": row["status"],
        "observed_at": row["observed_at"],
        "expires_at": row["expires_at"],
        "source": row["source"],
        "source_url": row["source_url"],
    }


def accumulate_streams(channels, streams, game_id, *, incomplete_error):
    duplicates = 0
    for stream in streams:
        if str(stream.get("game_id")) != game_id:
            raise incomplete_error(f"Category changed while enumerating game {game_id}")
        user_id, viewers = (str(stream.get("user_id") or ""), stream.get("viewer_count"))
        if not user_id or type(viewers) is not int or viewers < 0:
            raise incomplete_error(f"Invalid stream metrics for game {game_id}")
        if stream.get("type") != "live":
            raise incomplete_error(f"Non-live or invalid stream for game {game_id}")
        duplicates += int(user_id in channels)
        channels[user_id] = stream
    return duplicates


def census_metrics(channels, *, counter_type, median_fn):
    counts = [row["viewer_count"] for row in channels.values()]
    languages = counter_type((str(row.get("language") or "other") for row in channels.values()))
    return {
        "viewer_count": sum(counts),
        "streamer_count": len(counts),
        "median_viewer_count": median_fn(counts) if counts else None,
        "language_streamers": dict(languages.most_common()),
    }


def apply_release_hint_rows(
    rows,
    now,
    hints,
    *,
    datetime_type,
    timezone_type,
    timedelta_type,
    source_rules,
    timestamp,
):
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_date = row.get("first_release_date")
        date = (
            datetime_type.fromtimestamp(raw_date, timezone_type.utc)
            if type(raw_date) in (int, float)
            else None
        )
        age = now - date if date else None
        window = timedelta_type(days=source_rules["igdb_first_release_date"]["window_days"])
        band = (
            "unknown"
            if age is None
            else (
                "upcoming"
                if age < timedelta_type(0)
                else "recent_release" if age < window else "older_release"
            )
        )
        hints[str(row["id"])] = {
            "source": "IGDB",
            "checked_at": timestamp(now),
            "first_release_date": timestamp(date) if date else None,
            "release_band": band,
            "confirms_twitch_new_badge": False,
        }
