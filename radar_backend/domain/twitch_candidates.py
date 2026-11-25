"""Pure candidate observations, census validation and release metadata rules."""

from __future__ import annotations

from radar_backend.domain.twitch_audience import chinese_channels, chinese_metrics, streamer_counts_above_viewers
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
    chinese = [row["viewer_count"] for row in chinese_channels(channels.values())]
    languages = counter_type((str(row.get("language") or "other") for row in channels.values()))
    return {
        "viewer_count": sum(counts),
        "streamer_count": len(counts),
        "median_viewer_count": median_fn(counts) if counts else None,
        "streamer_counts_above_viewers": streamer_counts_above_viewers(counts),
        "language_streamers": dict(languages.most_common()),
        "audience_regions": {"zh": chinese_metrics(chinese, median_fn=median_fn)},
    }


# IGDB's date formats (release_dates.date_format, or the older category code).
DATE_FORMATS = {0: "YYYYMMMMDD", 1: "YYYYMMMM", 2: "YYYY", 3: "YYYYQ1",
                4: "YYYYQ2", 5: "YYYYQ3", 6: "YYYYQ4", 7: "TBD"}
DATE_PRECISION = {"YYYYMMMMDD": "day", "YYYYMMDD": "day", "YYYYMMMM": "month", "YYYYMM": "month",
                  "YYYYQ1": "quarter", "YYYYQ2": "quarter", "YYYYQ3": "quarter", "YYYYQ4": "quarter",
                  "YYYY": "year", "TBD": "unknown"}


def release_date_details(row, *, datetime_type, timezone_type, timestamp):
    """How exactly IGDB knows the first release date, and its exact Early
    Access and full release dates.

    IGDB stores "2027" as 2027-12-31 and a quarter as its last day; those
    are not launch days. Without release dates the precision stays unknown
    (None), as before IGDB precision was read.
    """
    rows = row.get("release_dates")
    if not isinstance(rows, list):
        return None, None, None
    first = row.get("first_release_date")
    precisions, early, full = set(), [], []
    for item in rows:
        if not isinstance(item, dict) or type(item.get("date")) not in (int, float):
            continue
        shape = item.get("date_format")
        name = shape.get("format") if isinstance(shape, dict) else None
        if not name and type(item.get("category")) is int:
            name = DATE_FORMATS.get(item["category"])
        precision = DATE_PRECISION.get(str(name or "").upper().replace(" ", ""), "unknown")
        if item["date"] == first:
            precisions.add(precision)
        status = item.get("status")
        status = str(status.get("name") if isinstance(status, dict) else "").strip().casefold()
        if precision == "day" and status == "early access":
            early.append(item["date"])
        elif precision == "day" and status == "full release":
            full.append(item["date"])

    def moment(values):
        return timestamp(datetime_type.fromtimestamp(min(values), timezone_type.utc)) if values else None

    first_precision = next((name for name in ("day", "month", "quarter", "year", "unknown") if name in precisions),
                           None)
    return first_precision, moment(early), moment(full)


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
        precision, early_access_at, full_release_at = release_date_details(
            row, datetime_type=datetime_type, timezone_type=timezone_type, timestamp=timestamp)
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
        if precision is not None:
            hints[str(row["id"])].update(release_precision=precision, early_access_at=early_access_at,
                                         full_release_at=full_release_at)

