"""Canonical snapshot composition with call-time validation and storage ports."""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path

from radar_backend.domain.twitch_newness import parse_timestamp, timestamp
from radar_backend.state.json_snapshot import write_json
from radar_backend.adapters.twitch_tracking import normalize_tracking_state
from radar_backend.state.validation import (
    validate_persisted_discovery,
    validate_persisted_mapping,
    validate_persisted_tracking,
)
from radar_backend.domain import twitch_snapshot as rules
from radar_backend.state.twitch_snapshot import store_snapshot as persist_snapshot

TAIPEI = timezone(timedelta(hours=8))
STATUS_PATH = "data/twitch_collection_status.json"
TRACKING_PATH = "data/twitch_tracking.json"
MAPPING_PATH = "data/twitch_steam_mapping.json"
DISCOVERY_PATH = "data/twitch_steam_discovery.json"


def merge_discovery_state(existing: dict | None, incoming: dict) -> dict:
    """Union intake provenance while keeping the latest successful ID decision.

    Public catalog membership changes on every metadata pass, independently of
    the cached IGDB lookup. Failed lookups have no new authoritative checked_at.
    """
    return rules.merge_discovery_state(
        existing,
        incoming,
        validate_persisted_discovery_fn=validate_persisted_discovery,
        datetime_type=datetime,
        timezone_value=timezone,
        parse_timestamp_fn=parse_timestamp,
        timestamp_fn=timestamp,
        deepcopy_fn=deepcopy,
    )


def merge_mapping_state(existing: dict | None, incoming: dict) -> dict:
    """Keep all AppID mappings while protecting newer mapping decisions."""
    return rules.merge_mapping_state(
        existing,
        incoming,
        validate_persisted_mapping_fn=validate_persisted_mapping,
        datetime_type=datetime,
        timezone_value=timezone,
        parse_timestamp_fn=parse_timestamp,
        timestamp_fn=timestamp,
        deepcopy_fn=deepcopy,
    )


def merge_tracking_state(existing: dict | None, incoming: dict) -> dict:
    """Union enrollments when publishing against a newer frontend checkout.

    A late result may contribute a previously unknown enrollment, but cannot
    rewind an entry's metadata, status or most recent real observation. A
    terminal status is not deleted: it remains a tombstone for later readers.
    """
    return rules.merge_tracking_state(
        existing,
        incoming,
        validate_persisted_tracking_fn=validate_persisted_tracking,
        parse_timestamp_fn=parse_timestamp,
        deepcopy_fn=deepcopy,
        timestamp_fn=timestamp,
        normalize_tracking_state_fn=normalize_tracking_state,
    )


def observed_rows(payload: dict) -> list[dict]:
    """Deduplicate discovery and enrolled games; only real observations belong in history."""
    return rules.observed_rows(payload)


def validate_schedule(payload: dict) -> dict | None:
    """New collectors use real observation time; old published hours stay intact."""
    return rules.validate_schedule(
        payload, parse_timestamp_fn=parse_timestamp, timestamp_fn=timestamp
    )


def observation_order(payload: dict) -> tuple[datetime, datetime]:
    """A late-published older sample must not replace a newer observation."""
    return rules.observation_order(payload, parse_timestamp_fn=parse_timestamp)


def validate_receipt(receipt: dict) -> tuple[datetime, datetime]:
    return rules.validate_receipt(
        receipt,
        parse_timestamp_fn=parse_timestamp,
        timestamp_fn=timestamp,
        taipei=TAIPEI,
    )


def validate_filtered_audience(value: dict, *, viewers: int, streamers: int) -> None:
    """Keep the new population explicit; incomplete lookups cannot claim a median."""
    return rules.validate_filtered_audience(
        value, viewers=viewers, streamers=streamers, math_module=math
    )


def validate_snapshot(payload: dict) -> None:
    return rules.validate_snapshot(
        payload,
        parse_timestamp_fn=parse_timestamp,
        validate_schedule_fn=validate_schedule,
        validate_persisted_tracking_fn=validate_persisted_tracking,
        validate_persisted_mapping_fn=validate_persisted_mapping,
        validate_persisted_discovery_fn=validate_persisted_discovery,
        observed_rows_fn=observed_rows,
        math_module=math,
        validate_filtered_audience_fn=validate_filtered_audience,
    )


def store_snapshot(payload: dict, frontend: Path) -> str:
    return persist_snapshot(
        payload,
        frontend,
        validate_snapshot_fn=validate_snapshot,
        parse_timestamp_fn=parse_timestamp,
        taipei=TAIPEI,
        timestamp_fn=timestamp,
        json_module=json,
        status_path_name=STATUS_PATH,
        tracking_path_name=TRACKING_PATH,
        mapping_path_name=MAPPING_PATH,
        discovery_path_name=DISCOVERY_PATH,
        validate_receipt_fn=validate_receipt,
        validate_persisted_tracking_fn=validate_persisted_tracking,
        merge_tracking_state_fn=merge_tracking_state,
        validate_persisted_mapping_fn=validate_persisted_mapping,
        merge_mapping_state_fn=merge_mapping_state,
        validate_persisted_discovery_fn=validate_persisted_discovery,
        merge_discovery_state_fn=merge_discovery_state,
        observation_order_fn=observation_order,
        write_json_fn=write_json,
        deepcopy_fn=deepcopy,
        observed_rows_fn=observed_rows,
        history_observation_fn=rules.history_observation,
        latest_observation_fn=rules.latest_observation,
        collection_receipt_fn=rules.collection_receipt,
    )
