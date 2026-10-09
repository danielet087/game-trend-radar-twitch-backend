"""Compose offline, source-labelled release-date experiments.

This module never requests Twitch's website or private GraphQL service.
Twitch dates must come from an explicitly dated response/dataset export.
IGDB is evaluated separately and never substituted for Twitch metadata.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from urllib.parse import urlsplit

from radar_backend.domain import twitch_newness as rules
from radar_backend.state import twitch_newness as storage

RELEASE_DATES_PATH = Path(__file__).resolve().parents[2] / "data/twitch_release_dates.json"
MAX_METADATA_AGE = timedelta(hours=24)
SOURCES = ("twitch_original_release_date", "igdb_first_release_date")
SOURCE_RULES = {
    SOURCES[0]: {"window_days": 14, "rule": "glance_release_age_lt_14_days_v1"},
    SOURCES[1]: {"window_days": 30, "rule": "igdb_release_age_lt_30_days_v1"},
}


def timestamp(value: datetime) -> str:
    return rules.timestamp(value, timezone_type=timezone)


def parse_timestamp(value: str) -> datetime:
    return rules.parse_timestamp(value, datetime_type=datetime, timezone_type=timezone)


def load_release_dates(path: str | Path = RELEASE_DATES_PATH) -> dict:
    return storage.load_release_dates(
        path,
        validate=lambda payload: rules.validate_release_dates(
            payload, timestamp=timestamp, parse_timestamp=parse_timestamp, urlsplit=urlsplit
        ),
        path_type=Path,
        json_module=json,
    )


def evaluate_date(
    release_at: str | None, observed_at: str | None, now: datetime, *, source: str
) -> dict:
    return rules.evaluate_date(
        release_at,
        observed_at,
        now,
        source=source,
        timestamp=timestamp,
        parse_timestamp=parse_timestamp,
        source_rules=SOURCE_RULES,
        max_metadata_age=MAX_METADATA_AGE,
        timedelta_type=timedelta,
    )


def attach_experiments(
    candidates: list[dict],
    excluded: list[dict],
    hints: dict,
    dates: dict,
    now: datetime,
    *,
    igdb_predictions: dict[str, dict] | None = None,
) -> dict:
    return rules.attach_experiments(
        candidates,
        excluded,
        hints,
        dates,
        now,
        igdb_predictions=igdb_predictions,
        evaluate_date=evaluate_date,
        parse_timestamp=parse_timestamp,
        sources=SOURCES,
        source_rules=SOURCE_RULES,
        timedelta_type=timedelta,
    )
