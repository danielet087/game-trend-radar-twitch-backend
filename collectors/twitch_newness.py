"""Offline, source-labelled trial of Glance's release-date NEW heuristic.

This module never requests Twitch's website or private GraphQL service.
Twitch dates must come from an explicitly dated response/dataset export.
IGDB is evaluated separately and never substituted for Twitch metadata.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from urllib.parse import urlsplit

RELEASE_DATES_PATH = Path(__file__).resolve().parents[1] / "data/twitch_release_dates.json"
MAX_METADATA_AGE = timedelta(hours=24)
SOURCES = ("twitch_original_release_date", "igdb_first_release_date")
SOURCE_RULES = {
    SOURCES[0]: {"window_days": 14, "rule": "glance_release_age_lt_14_days_v1"},
    SOURCES[1]: {"window_days": 30, "rule": "igdb_release_age_lt_30_days_v1"},
}


def timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Timestamp must be a string with a timezone")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timestamp must include a timezone")
    return result.astimezone(timezone.utc)


def load_release_dates(path: str | Path = RELEASE_DATES_PATH) -> dict:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1 or not isinstance(payload.get("records"), dict):
        raise ValueError("Invalid Twitch release-date export")
    clean = {}
    for game_id, row in payload["records"].items():
        if not game_id.isdigit() or not isinstance(row, dict) or row.get("source_field") != "originalReleaseDate":
            raise ValueError("Release records require a Twitch category ID and originalReleaseDate provenance")
        if not isinstance(row.get("source_name"), str) or not row["source_name"].strip():
            raise ValueError("Release records require a source name")
        url = urlsplit(row.get("source_url", ""))
        if url.scheme != "https" or not url.netloc or url.username or url.password or url.query or url.fragment:
            raise ValueError("Source URL must be public HTTPS without credentials, query or fragment")
        # Copy only the declared metadata; never propagate raw HTTP headers or tokens.
        clean[game_id] = {
            "original_release_date": timestamp(parse_timestamp(row["original_release_date"])),
            "observed_at": timestamp(parse_timestamp(row["observed_at"])),
            "source_field": "originalReleaseDate", "source_name": row["source_name"],
            "source_url": row["source_url"],
        }
    return clean


def evaluate_date(release_at: str | None, observed_at: str | None, now: datetime, *, source: str) -> dict:
    rule = SOURCE_RULES[source]
    window = timedelta(days=rule["window_days"])
    result = {
        "source": source, "evaluated_at": timestamp(now), **rule,
        "status": "unknown", "predicted_new": None,
        "confirms_twitch_new_badge": False,
    }
    if not release_at or not observed_at:
        return {**result, "reason": "missing_release_date"}
    try:
        released, observed = parse_timestamp(release_at), parse_timestamp(observed_at)
    except (ValueError, TypeError):
        return {**result, "reason": "invalid_release_metadata"}
    result.update(release_at=timestamp(released), metadata_observed_at=timestamp(observed))
    if not observed <= now < observed + MAX_METADATA_AGE:
        return {**result, "reason": "metadata_expired_or_future"}
    age = now - released
    return {
        **result, "status": "evaluated", "predicted_new": age < window,
        "release_phase": "upcoming" if age < timedelta(0) else f"released_within_{rule['window_days']}_days" if age < window else "older_release",
        "age_hours": round(age.total_seconds() / 3600, 3),
    }


def attach_experiments(candidates: list[dict], excluded: list[dict], hints: dict, dates: dict, now: datetime,
                       *, igdb_predictions: dict[str, dict] | None = None) -> dict:
    reference_checks = []
    for row in candidates + excluded:
        game_id = row["game_id"]
        date = dates.get(game_id, {})
        hint = hints.get(str(row.get("igdb_id")), {})
        twitch = evaluate_date(date.get("original_release_date"), date.get("observed_at"), now, source=SOURCES[0])
        if date:
            twitch.update({key: date[key] for key in ("source_name", "source_url", "source_field")})
        # Reuse the exact dated collection decision. A game crossing 30 days
        # while its streams are measured must not acquire a contradictory
        # "miss" result immediately before we enrich the accepted sample.
        igdb = (dict(igdb_predictions[game_id]) if igdb_predictions is not None and game_id in igdb_predictions
                else evaluate_date(hint.get("first_release_date"), hint.get("checked_at"), now, source=SOURCES[1]))
        row["release_experiment"] = dict(zip(SOURCES, (twitch, igdb)))
        verification = row.get("verification", {})
        if verification.get("status") not in {"new", "not_new"}:
            continue
        # Compare at the badge's timestamp, not at the later collection time.
        # A later metadata capture makes this retrospective, not a validation.
        badge_at = parse_timestamp(verification["observed_at"])
        for source, prediction in row["release_experiment"].items():
            if prediction["status"] != "evaluated":
                continue
            at_badge_time = badge_at - parse_timestamp(prediction["release_at"]) < timedelta(days=prediction["window_days"])
            reference_checks.append({
                "game_id": game_id, "game_name": row["game_name"], "source": source,
                "window_days": prediction["window_days"], "rule": prediction["rule"],
                "badge_status": verification["status"], "badge_observed_at": verification["observed_at"],
                "metadata_observed_at": prediction["metadata_observed_at"],
                "predicted_new_at_badge_time": at_badge_time,
                "agrees_with_reference": at_badge_time == (verification["status"] == "new"),
                "comparison_kind": "same_timestamp" if parse_timestamp(prediction["metadata_observed_at"]) == badge_at else "retrospective",
            })
    report = {
        "rule": "source_specific_release_age_v2",
        "includes_future_releases": True, "experimental": True,
        "confirms_twitch_new_badge": False,
        "twitch_date_records_loaded": len(dates),
        "reference_checks": reference_checks,
        "comparison_note": "Retrospective checks apply later release metadata to an earlier badge observation; agreement does not validate Twitch's official rule.",
    }
    for source in SOURCES:
        evaluated = [r for r in candidates if r["release_experiment"][source]["status"] == "evaluated"]
        evaluated_excluded = [r for r in excluded if r["release_experiment"][source]["status"] == "evaluated"]
        report[source] = {
            **SOURCE_RULES[source],
            "evaluated_candidates": len(evaluated), "unknown_candidates": len(candidates) - len(evaluated),
            "evaluated_excluded": len(evaluated_excluded), "unknown_excluded": len(excluded) - len(evaluated_excluded),
            "predicted_new_game_ids": [r["game_id"] for r in evaluated if r["release_experiment"][source]["predicted_new"]],
            "upcoming_game_ids": [r["game_id"] for r in evaluated if r["release_experiment"][source]["release_phase"] == "upcoming"],
        }
    return report
