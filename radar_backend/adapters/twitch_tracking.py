"""Tracking composition using canonical rules and current helper callbacks."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta

from radar_backend.domain.twitch_newness import parse_timestamp, timestamp
from radar_backend.domain import twitch_tracking as rules

TRACKING_DAYS = 30
STATUSES = {"active", "expired", "excluded"}


def _sources(entry: dict) -> dict:
    """Migrate the old single-source registry without rewriting its evidence."""
    return rules._sources(entry, deepcopy_fn=deepcopy)


def normalize_tracking_state(
    payload: dict | None, now: datetime | None = None, *, non_game_ids=()
) -> dict:
    """Validate and copy entries; migrate legacy evidence and reconcile expiry."""
    return rules.normalize_tracking_state(
        payload,
        now,
        non_game_ids=non_game_ids,
        deepcopy_fn=deepcopy,
        timestamp_fn=timestamp,
        parse_timestamp_fn=parse_timestamp,
        sources_fn=_sources,
        reconcile_tracking_entry_fn=reconcile_tracking_entry,
        statuses=STATUSES,
    )


def release_from_observation(row: dict) -> tuple[str | None, str | None]:
    """Release cache age controls admission, not a known membership's expiry."""
    return rules.release_from_observation(
        row, timestamp_fn=timestamp, parse_timestamp_fn=parse_timestamp
    )


def reconcile_tracking_entry(
    entry: dict,
    now: datetime,
    *,
    release_at: str | None = None,
    release_source: str | None = None,
    non_game_ids=(),
) -> dict:
    return rules.reconcile_tracking_entry(
        entry,
        now,
        release_at=release_at,
        release_source=release_source,
        non_game_ids=non_game_ids,
        deepcopy_fn=deepcopy,
        timestamp_fn=timestamp,
        parse_timestamp_fn=parse_timestamp,
        sources_fn=_sources,
        tracking_days=TRACKING_DAYS,
        timedelta_type=timedelta,
    )


def admission_evidence(row: dict, observed_at: datetime) -> str | None:
    """Replay labelled Twitch evidence, including former 14-day IGDB records."""
    return rules.admission_evidence(
        row,
        observed_at,
        parse_timestamp_fn=parse_timestamp,
        release_from_observation_fn=release_from_observation,
        tracking_days=TRACKING_DAYS,
        timedelta_type=timedelta,
    )


def tracking_metadata(entry: dict) -> dict:
    return rules.tracking_metadata(entry, deepcopy_fn=deepcopy)


def enroll_steam_mapping(
    state: dict, mapping: dict, now: datetime, *, non_game_ids=()
) -> dict | None:
    """Enroll one confirmed Steam release without requiring live viewers."""
    return rules.enroll_steam_mapping(
        state,
        mapping,
        now,
        non_game_ids=non_game_ids,
        deepcopy_fn=deepcopy,
        timestamp_fn=timestamp,
        parse_timestamp_fn=parse_timestamp,
        sources_fn=_sources,
        reconcile_tracking_entry_fn=reconcile_tracking_entry,
        tracking_days=TRACKING_DAYS,
        timedelta_type=timedelta,
    )


def reconcile_steam_catalog(
    state: dict,
    catalog: list[dict],
    mappings: dict,
    now: datetime,
    *,
    non_game_ids=(),
) -> None:
    """Reconcile only a successfully loaded catalog; failures never remove it."""
    return rules.reconcile_steam_catalog(
        state,
        catalog,
        mappings,
        now,
        non_game_ids=non_game_ids,
        deepcopy_fn=deepcopy,
        timestamp_fn=timestamp,
        sources_fn=_sources,
        reconcile_tracking_entry_fn=reconcile_tracking_entry,
        enroll_steam_mapping_fn=enroll_steam_mapping,
    )


def enroll_observation(
    state: dict,
    row: dict,
    observed_at: datetime,
    *,
    min_viewers: int = 7000,
    non_game_ids=(),
    eligibility_at: datetime | None = None,
    allow_twitch_enrollment: bool = True,
) -> dict | None:
    """Add qualified Twitch membership, then store one real census observation."""
    return rules.enroll_observation(
        state,
        row,
        observed_at,
        min_viewers=min_viewers,
        non_game_ids=non_game_ids,
        eligibility_at=eligibility_at,
        allow_twitch_enrollment=allow_twitch_enrollment,
        deepcopy_fn=deepcopy,
        timestamp_fn=timestamp,
        sources_fn=_sources,
        reconcile_tracking_entry_fn=reconcile_tracking_entry,
        release_from_observation_fn=release_from_observation,
        admission_evidence_fn=admission_evidence,
        tracking_metadata_fn=tracking_metadata,
    )
