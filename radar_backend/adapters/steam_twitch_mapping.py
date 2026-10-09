"""Compose authoritative Steam/IGDB/Twitch mapping rules and source ports."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import re
import time
from typing import Callable
from zoneinfo import ZoneInfo

import requests
from urllib3.util import Timeout

from radar_backend.application import steam_twitch_mapping as _application
from radar_backend.domain import steam_twitch_mapping as _rules
from radar_backend.adapters import igdb_identity as _transport
from radar_backend.domain.twitch import CollectionDeadlineExceeded
from radar_backend.domain.twitch_newness import parse_timestamp, timestamp
from radar_core.domain.twitch_admission import (
    TW_STORE_DATE_AUTHORITY,
    has_taiwan_store_date_authority,
    is_twitch_qualified,
)

TAIPEI = ZoneInfo("Asia/Taipei")
METHOD = "steam_appid_igdb_external_games_helix_igdb_id"
IGDB_BASE = "https://api.igdb.com/v4/"
RETRY_INTERVAL = timedelta(hours=24)
DAY = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


def _id(value) -> str:
    return _rules._id(value)


def _now(now: datetime | None) -> datetime:
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        raise ValueError("Clock must include a timezone")
    return clock.astimezone(timezone.utc)


def normalize_mapping_state(payload: dict | None) -> dict:
    """Validate persisted identity links without reinterpreting their statuses."""
    return _rules.normalize_mapping_state(
        payload,
        deepcopy=lambda value: deepcopy(value),
        parse_timestamp=lambda value: parse_timestamp(value),
        _id=lambda value: _id(value),
        METHOD=METHOD,
    )


def _strings(value) -> list[str]:
    return _rules._strings(value)


def _labels(value, allowed: list[str]) -> dict[str, str]:
    return _rules._labels(value, allowed)


def normalize_steam_catalog(payload: dict, now: datetime) -> list[dict]:
    """Read the curated public catalog; uncertain/conflicting release rows cannot enroll.

    An exact UTC release instant takes precedence over Taipei midnight. Future
    releases remain useful for identity links, but are never recent releases."""
    clock = _now(now)
    return _rules.normalize_steam_catalog(
        payload,
        clock,
        parse_timestamp=lambda value: parse_timestamp(value),
        _id=lambda value: _id(value),
        has_taiwan_store_date_authority=lambda row: has_taiwan_store_date_authority(
            row
        ),
        is_twitch_qualified=lambda row: is_twitch_qualified(row),
        DAY=DAY,
        date=date,
        datetime=datetime,
        TAIPEI=TAIPEI,
        timezone=timezone,
        _strings=lambda value: _strings(value),
        _labels=lambda *args: _labels(*args),
        timedelta=timedelta,
        timestamp=lambda value: timestamp(value),
        TW_STORE_DATE_AUTHORITY=TW_STORE_DATE_AUTHORITY,
        deepcopy=lambda value: deepcopy(value),
        _clock=lambda value: value,
    )


def _deadline(deadline: float | None, monotonic: Callable[[], float]) -> None:
    return _transport._deadline(
        deadline, monotonic, CollectionDeadlineExceeded=CollectionDeadlineExceeded
    )


def _igdb(client, endpoint: str, query: str, deadline, monotonic) -> list[dict]:
    return _transport._igdb(
        client,
        endpoint,
        query,
        deadline,
        monotonic,
        _deadline=lambda *args: _deadline(*args),
        Timeout=Timeout,
        IGDB_BASE=IGDB_BASE,
    )


def _pages(client, endpoint: str, query: str, deadline, monotonic) -> list[dict]:
    return _transport._pages(
        client,
        endpoint,
        query,
        deadline,
        monotonic,
        _igdb=lambda *args: _igdb(*args),
    )


def _error(exc: Exception) -> str:
    return _transport._error(exc)


def _reuse_discovery_mappings(
    state: dict,
    catalog: list[dict],
    discovery_state: dict | None,
    tracking_state: dict | None,
    clock: datetime,
) -> set[str]:
    """Reuse the same validated official ID chain without another API lookup.

    Reverse intake may know a link before the game enters the curated catalog.
    Only its current, qualified Twitch membership and a unique AppID identity
    can seed a forward cache. A conflicting or newer forward decision wins."""
    if discovery_state is None or tracking_state is None:
        return set()
    # Resolve the reverse-side ports only when both inputs exist, as before.
    from radar_backend.adapters.twitch_steam_discovery import (
        NON_GAME_IDS,
        _qualified_members,
        normalize_discovery_state,
    )
    from radar_backend.adapters.twitch_tracking import normalize_tracking_state

    return _rules._reuse_discovery_mappings(
        state,
        catalog,
        discovery_state,
        tracking_state,
        clock,
        normalize_discovery_state=normalize_discovery_state,
        normalize_tracking_state=normalize_tracking_state,
        _qualified_members=_qualified_members,
        NON_GAME_IDS=NON_GAME_IDS,
        parse_timestamp=lambda value: parse_timestamp(value),
        _id=lambda value: _id(value),
        deepcopy=lambda value: deepcopy(value),
        METHOD=METHOD,
    )


def refresh_mappings(
    client,
    catalog: dict | list[dict],
    mapping_state: dict | None = None,
    now: datetime | None = None,
    *,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    discovery_state: dict | None = None,
    tracking_state: dict | None = None,
    allow_lookup: bool = True,
) -> dict:
    """Refresh uncached links, preserving confirmed links during API outages.

    Unmatched/ambiguous links are retried daily. Confirmed IDs are reused while
    the latest Steam metadata and its release window are refreshed every run."""
    return _application.refresh_mappings(
        client,
        catalog,
        mapping_state,
        now,
        deadline=deadline,
        monotonic=monotonic,
        discovery_state=discovery_state,
        tracking_state=tracking_state,
        allow_lookup=allow_lookup,
        _now=lambda value: _now(value),
        normalize_mapping_state=lambda value: normalize_mapping_state(value),
        normalize_steam_catalog=lambda *args: normalize_steam_catalog(*args),
        deepcopy=lambda value: deepcopy(value),
        _reuse_discovery_mappings=lambda *args: _reuse_discovery_mappings(*args),
        _id=lambda value: _id(value),
        parse_timestamp=lambda value: parse_timestamp(value),
        timedelta=timedelta,
        TAIPEI=TAIPEI,
        timestamp=lambda value: timestamp(value),
        METHOD=METHOD,
        failure_types=lambda: (
            requests.RequestException,
            ValueError,
            KeyError,
            TypeError,
            RuntimeError,
            OverflowError,
            OSError,
        ),
        _pages=lambda *args: _pages(*args),
        _error=lambda value: _error(value),
        _deadline=lambda *args: _deadline(*args),
        RETRY_INTERVAL=RETRY_INTERVAL,
        CollectionDeadlineExceeded=CollectionDeadlineExceeded,
        re=re,
        Counter=lambda *args: Counter(*args),
    )
