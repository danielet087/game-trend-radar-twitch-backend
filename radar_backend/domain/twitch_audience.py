"""Pure follower freshness, eligibility and ordering rules."""

from __future__ import annotations

from datetime import datetime, timedelta

RULE = "followers_gt_1000_viewers_gte_10_v1"
MAX_AGE = timedelta(hours=24)
MIN_FOLLOWERS = 1000
MIN_VIEWERS = 10


def _fresh_record(
    row: object, now: datetime, *, parse_timestamp, timestamp, max_age
) -> dict | None:
    if not isinstance(row, dict) or type(row.get("total")) is not int or row["total"] < 0:
        return None
    try:
        observed = parse_timestamp(row.get("observed_at"))
    except (ValueError, TypeError):
        return None
    if not observed <= now < observed + max_age:
        return None
    return {"total": row["total"], "observed_at": timestamp(observed)}


def priority(row):
    experiments = row.get("release_experiment", {})
    signal = any(
        (
            isinstance(trial, dict) and trial.get("predicted_new") is True
            for trial in experiments.values()
        )
    )
    rank = 0 if row.get("verification", {}).get("status") == "new" else 1 if signal else 2
    return (rank, -row["viewer_count"], row["game_id"])


def summarize_metrics(
    eligible, low_viewers, low_followers, unknown, *, rule, min_followers, min_viewers, median_fn
):
    return {
        "rule": rule,
        "min_followers_exclusive": min_followers,
        "min_viewers_inclusive": min_viewers,
        "followers_max_age_hours": 24,
        "status": "partial" if unknown else "complete",
        "median_viewer_count": median_fn(eligible) if eligible and not unknown else None,
        "eligible_streamer_count": len(eligible),
        "eligible_viewer_count": sum(eligible),
        "excluded_low_viewer_count": low_viewers,
        "excluded_low_follower_count": low_followers,
        "unknown_follower_count": unknown,
    }
