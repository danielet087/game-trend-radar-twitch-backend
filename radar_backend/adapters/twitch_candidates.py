"""Canonical candidate collection composition."""

from __future__ import annotations
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import logging
import math
from pathlib import Path
from statistics import median
import time
from typing import Any, Callable
import requests
from urllib3.util import Timeout
from radar_backend.adapters.twitch_http import CollectionDeadlineExceeded, TwitchClient
from radar_backend.adapters.twitch_audience import (
    CACHE_PATH,
    FollowerResolver,
    attach_filtered_audience,
)
from radar_backend.adapters.twitch_newness import (
    RELEASE_DATES_PATH,
    SOURCE_RULES,
    attach_experiments,
    evaluate_date,
    load_release_dates,
    parse_timestamp,
    timestamp,
)
from radar_backend.adapters.twitch_tracking import (
    enroll_observation,
    normalize_tracking_state,
    reconcile_tracking_entry,
    reconcile_steam_catalog,
)
from radar_backend.application import twitch_candidates as _application
from radar_backend.domain import twitch_candidates as _rules
from radar_backend.adapters import igdb_release_hints as _igdb
from radar_backend.state import twitch_candidates as _state
from radar_backend.domain.twitch_candidates import IncompleteCollection

LOGGER = logging.getLogger(__name__)
REGISTRY_PATH = Path(__file__).resolve().parents[2] / "data/twitch_category_verification.json"
IGDB_URL = "https://api.igdb.com/v4/games"
NON_GAME_IDS = _rules.NON_GAME_IDS


def load_verifications(path: str | Path = REGISTRY_PATH) -> dict[str, dict[str, Any]]:
    return _state.load_verifications(
        path,
        path_type=Path,
        json_module=json,
        validate=_rules.validate_verifications,
        parse_timestamp=parse_timestamp,
        timedelta_type=timedelta,
    )


def verification_for(game_id: str, observations: dict, now: datetime) -> dict:
    return _rules.verification_for(game_id, observations, now, parse_timestamp=parse_timestamp)


class PageReader:

    def __init__(
        self,
        client: TwitchClient,
        max_calls: int,
        *,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        _application.initialize_page_reader(
            self, client, max_calls, deadline=deadline, monotonic=monotonic
        )

    def get(self, endpoint: str, params: dict) -> tuple[list[dict], str | None]:
        return _application.read_page(
            self,
            endpoint,
            params,
            incomplete_error=IncompleteCollection,
            deadline_error=CollectionDeadlineExceeded,
        )


def category_metrics(
    reader: PageReader, game_id: str, max_pages: int = 150, *, retain_channels: bool = False
) -> dict:
    return _application.category_metrics(
        reader,
        game_id,
        max_pages,
        retain_channels=retain_channels,
        utcnow=lambda: datetime.now(timezone.utc),
        timestamp=timestamp,
        accumulate_streams=_rules.accumulate_streams,
        census_metrics=_rules.census_metrics,
        counter_type=Counter,
        median_fn=median,
        incomplete_error=IncompleteCollection,
    )


def release_hints(
    client: TwitchClient,
    games: list[dict],
    now: datetime,
    *,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[dict, str]:
    return _igdb.release_hints(
        client,
        games,
        now,
        deadline=deadline,
        monotonic=monotonic,
        igdb_url=IGDB_URL,
        datetime_type=datetime,
        timezone_type=timezone,
        timedelta_type=timedelta,
        timeout_type=Timeout,
        source_rules=SOURCE_RULES,
        timestamp=timestamp,
        logger=LOGGER,
        request_error=requests.RequestException,
        deadline_error=CollectionDeadlineExceeded,
        apply_rows=_rules.apply_release_hint_rows,
    )


def _mapping_callbacks():
    from collectors.steam_twitch_mapping import (
        normalize_mapping_state,
        normalize_steam_catalog,
        refresh_mappings,
    )

    return (normalize_mapping_state, normalize_steam_catalog, refresh_mappings)


def _discovery_callback():
    from collectors.twitch_steam_discovery import refresh_discoveries

    return refresh_discoveries


def collect_candidates(
    *,
    client_id: str,
    client_secret: str,
    min_viewers: int = 7000,
    max_category_pages: int = 5,
    max_stream_pages: int = 150,
    max_api_calls: int = 1200,
    registry_path: str | Path = REGISTRY_PATH,
    include_release_hints: bool = True,
    release_dates_path: str | Path = RELEASE_DATES_PATH,
    client: TwitchClient | None = None,
    now: datetime | None = None,
    category_page_size: int = 100,
    include_filtered_audience: bool = False,
    followers_cache_path: str | Path = CACHE_PATH,
    followers_max_calls: int | None = None,
    followers_max_seconds: float | None = None,
    max_collection_seconds: float = 1500,
    monotonic: Callable[[], float] = time.monotonic,
    tracking_state: dict | None = None,
    steam_catalog: dict | None = None,
    steam_mapping_state: dict | None = None,
    steam_discovery_state: dict | None = None,
) -> dict[str, Any]:
    return _application.collect_candidates(
        client_id=client_id,
        client_secret=client_secret,
        min_viewers=min_viewers,
        max_category_pages=max_category_pages,
        max_stream_pages=max_stream_pages,
        max_api_calls=max_api_calls,
        registry_path=registry_path,
        include_release_hints=include_release_hints,
        release_dates_path=release_dates_path,
        client=client,
        now=now,
        category_page_size=category_page_size,
        include_filtered_audience=include_filtered_audience,
        followers_cache_path=followers_cache_path,
        followers_max_calls=followers_max_calls,
        followers_max_seconds=followers_max_seconds,
        max_collection_seconds=max_collection_seconds,
        monotonic=monotonic,
        tracking_state=tracking_state,
        steam_catalog=steam_catalog,
        steam_mapping_state=steam_mapping_state,
        steam_discovery_state=steam_discovery_state,
        datetime_type=datetime,
        timezone_type=timezone,
        math_module=math,
        normalize_tracking_state=normalize_tracking_state,
        load_verifications=load_verifications,
        load_release_dates=load_release_dates,
        client_type=TwitchClient,
        page_reader=PageReader,
        mapping_callbacks=_mapping_callbacks,
        reconcile_steam_catalog=reconcile_steam_catalog,
        evaluate_date=evaluate_date,
        reconcile_tracking_entry=reconcile_tracking_entry,
        timestamp=timestamp,
        non_game_ids=NON_GAME_IDS,
        release_hints=release_hints,
        verification_for=verification_for,
        category_metrics=category_metrics,
        logger=LOGGER,
        attach_experiments=attach_experiments,
        follower_resolver=FollowerResolver,
        attach_filtered_audience=attach_filtered_audience,
        enroll_observation=enroll_observation,
        parse_timestamp=parse_timestamp,
        discovery_callback=_discovery_callback,
        incomplete_error=IncompleteCollection,
    )
