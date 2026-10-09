"""Canonical reverse discovery composition and public compatibility contract."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta
import time
from typing import Callable

import requests

from radar_backend.adapters.steam_twitch_mapping import (
    _deadline,
    _error,
    _id,
    _now,
    _pages,
)
from radar_backend.domain.twitch import CollectionDeadlineExceeded
from radar_backend.domain.twitch_newness import parse_timestamp, timestamp
from radar_backend.domain.twitch_tracking import normalize_tracking_state
from radar_backend.domain import twitch_steam_discovery as _rules
from radar_backend.application import twitch_steam_discovery as _application

METHOD = "twitch_igdb_external_steam_v1"
POLICY_VERSION = 2
TWITCH_IDENTITY_METHOD = "igdb_external_twitch_uid_v1"
RETRY_INTERVAL = timedelta(hours=24)
STATUSES = {"matched", "no_steam_link", "pending", "unavailable"}
ENROLLMENT_SOURCES = {
    "igdb_first_release_date",
    "twitch_original_release_date",
    "twitch_directory_dom",
}
NON_GAME_IDS = {"509658", "509672", "509663", "509659", "26936"}
FAILURES = (
    requests.RequestException,
    ValueError,
    KeyError,
    TypeError,
    RuntimeError,
    OverflowError,
    OSError,
)


def _normalize_website_identity(value, twitch_id, igdb_id):
    from radar_backend.adapters.twitch_steam_website_identity import (
        normalize_website_identity,
    )

    return normalize_website_identity(value, twitch_id, igdb_id)


def _website_lookup():
    from radar_backend.adapters.twitch_steam_website_identity import (
        lookup_website_identity,
    )

    return lookup_website_identity


def _ids(value) -> list[str]:
    return _rules._ids(value, id_fn=_id)


def _enrollment(value, clock: datetime | None = None) -> dict:
    return _rules._enrollment(
        value,
        clock,
        enrollment_sources=ENROLLMENT_SOURCES,
        parse_timestamp_fn=parse_timestamp,
        deepcopy_fn=deepcopy,
    )


def _twitch_identity(value: dict, twitch_id: str, igdb_id: str | None) -> dict:
    """Validate the exact official Twitch UID fallback, never a name match."""
    return _rules._twitch_identity(
        value,
        twitch_id,
        igdb_id,
        identity_method=TWITCH_IDENTITY_METHOD,
        id_fn=_id,
        parse_timestamp_fn=parse_timestamp,
        deepcopy_fn=deepcopy,
    )


def _invalidate_identity(row: dict, reason: str, at: str, retry_at: str) -> None:
    """A completed contradictory lookup invalidates an old positive decision."""
    return _rules._invalidate_identity(
        row, reason, at, retry_at, policy_version=POLICY_VERSION
    )


def normalize_discovery_state(payload: dict | None = None) -> dict:
    """Copy and validate persisted identities and their exact official evidence."""
    return _rules.normalize_discovery_state(
        payload,
        deepcopy_fn=deepcopy,
        parse_timestamp_fn=parse_timestamp,
        id_fn=_id,
        ids_fn=_ids,
        statuses=STATUSES,
        method=METHOD,
        enrollment_fn=_enrollment,
        policy_version=POLICY_VERSION,
        twitch_identity_fn=_twitch_identity,
        website_normalizer=_normalize_website_identity,
    )


def _catalog_ids(payload: dict) -> set[str]:
    return _rules._catalog_ids(payload, parse_timestamp_fn=parse_timestamp, id_fn=_id)


def _qualified_members(tracking: dict, clock: datetime) -> dict[str, tuple[dict, dict]]:
    return _rules._qualified_members(
        tracking, clock, non_game_ids=NON_GAME_IDS, enrollment_fn=_enrollment, id_fn=_id
    )


def refresh_discoveries(
    client,
    tracking_state: dict,
    public_catalog: dict,
    discovery_state: dict | None = None,
    now: datetime | None = None,
    *,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict:
    """Find Steam products for genuinely enrolled Twitch discoveries.

    Successful identity decisions are cached for 24 hours. API failures retain
    the previous decision and timestamp, allowing the next hourly run to retry.
    Public catalog membership is metadata, refreshed separately on every run.
    """
    return _application.refresh_discoveries(
        client,
        tracking_state,
        public_catalog,
        discovery_state,
        now,
        deadline=deadline,
        monotonic=monotonic,
        _now=_now,
        timestamp=timestamp,
        _catalog_ids=_catalog_ids,
        normalize_tracking_state=normalize_tracking_state,
        NON_GAME_IDS=NON_GAME_IDS,
        _qualified_members=_qualified_members,
        normalize_discovery_state=normalize_discovery_state,
        METHOD=METHOD,
        parse_timestamp=parse_timestamp,
        POLICY_VERSION=POLICY_VERSION,
        _error=_error,
        _deadline=_deadline,
        _id=_id,
        FAILURES=FAILURES,
        CollectionDeadlineExceeded=CollectionDeadlineExceeded,
        _pages=_pages,
        _invalidate_identity=_invalidate_identity,
        RETRY_INTERVAL=RETRY_INTERVAL,
        TWITCH_IDENTITY_METHOD=TWITCH_IDENTITY_METHOD,
        deepcopy=deepcopy,
        Counter=Counter,
        website_lookup=_website_lookup,
    )
