"""Compose follower audience rules, HTTP and runner-local cache ports."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import math
import os
from pathlib import Path
from statistics import median
import tempfile
import time
from typing import Callable

import requests
from urllib3.util import Timeout

from radar_backend.application import twitch_audience as _application
from radar_backend.domain import twitch_audience as _rules
from radar_backend.adapters import twitch_followers as _transport
from radar_backend.state import twitch_followers as _cache
from radar_backend.adapters.twitch_http import TWITCH_API_BASE, TwitchClient
from radar_backend.domain.twitch_newness import parse_timestamp, timestamp

LOGGER = logging.getLogger("collectors.twitch_audience")
RULE = _rules.RULE
CACHE_PATH = Path(".cache/twitch_followers.json")
MAX_AGE = _rules.MAX_AGE
MIN_FOLLOWERS = _rules.MIN_FOLLOWERS
MIN_VIEWERS = _rules.MIN_VIEWERS


def _fresh_record(row: object, now: datetime) -> dict | None:
    return _rules._fresh_record(
        row, now, parse_timestamp=parse_timestamp, timestamp=timestamp, max_age=MAX_AGE
    )


class FollowerResolver(_application.FollowerResolver):
    def __init__(
        self,
        client: TwitchClient,
        cache_path: str | Path = CACHE_PATH,
        *,
        max_calls: int | None = None,
        max_seconds: float | None = None,
        collection_deadline: float | None = None,
        utcnow: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        super().__init__(
            client,
            cache_path,
            max_calls=max_calls,
            max_seconds=max_seconds,
            collection_deadline=collection_deadline,
            utcnow=utcnow or (lambda: datetime.now(timezone.utc)),
            monotonic=monotonic,
            sleep=sleep,
            path_type=lambda value: Path(value),
            load_cache=lambda path: _cache.load(path, json_module=json),
            save_cache=lambda path, rows: _cache.save(
                path,
                rows,
                path_type=lambda value: Path(value),
                temporary_file=tempfile.NamedTemporaryFile,
                replace=os.replace,
                json_module=json,
                warning=lambda *args: LOGGER.warning(*args),
            ),
            fresh_record=lambda row, now: _fresh_record(row, now),
            timestamp=lambda value: timestamp(value),
            request=lambda current_client, user_id, remaining: _transport.request_followers(
                current_client,
                user_id,
                remaining,
                api_base=TWITCH_API_BASE,
                timeout_type=Timeout,
                isfinite=math.isfinite,
            ),
            request_exception=lambda: requests.RequestException,
            warning=lambda *args, **kwargs: LOGGER.warning(*args, **kwargs),
            math_module=lambda: math,
        )


def filtered_metrics(channels: list[dict], resolver: FollowerResolver) -> dict:
    return _application.filtered_metrics(
        channels,
        resolver,
        min_viewers=lambda: MIN_VIEWERS,
        min_followers=lambda: MIN_FOLLOWERS,
        summarize_metrics=lambda eligible, low_viewers, low_followers, unknown: _rules.summarize_metrics(
            eligible,
            low_viewers,
            low_followers,
            unknown,
            rule=RULE,
            min_followers=MIN_FOLLOWERS,
            min_viewers=MIN_VIEWERS,
            median_fn=median,
        ),
    )


def attach_filtered_audience(
    candidates: list[dict], channels_by_game: dict[str, list[dict]], resolver: FollowerResolver
) -> dict:
    return _application.attach_filtered_audience(
        candidates,
        channels_by_game,
        resolver,
        priority=_rules.priority,
        filtered_metrics=lambda *args: filtered_metrics(*args),
        info=lambda *args: LOGGER.info(*args),
        rule=lambda: RULE,
    )
