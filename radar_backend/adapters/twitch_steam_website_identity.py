"""Compose pure website evidence rules, exact IGDB lookups and keyless Steam HTTP."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
import re
import time
from typing import Callable
from urllib.parse import urlsplit

import requests
from urllib3.util import Timeout

from radar_backend.application import twitch_steam_website_identity as _application
from radar_backend.domain import twitch_steam_website_identity as _rules
from radar_backend.adapters import steam_identity_http as _transport
from radar_backend.adapters.steam_twitch_mapping import _deadline, _id, _now, _pages
from radar_backend.domain.twitch_newness import parse_timestamp, timestamp

METHOD = _rules.METHOD
PROVIDER = _rules.PROVIDER
APPDETAILS_URL = _rules.APPDETAILS_URL
STORE_HOST = _rules.STORE_HOST
APP_PATH = _rules.APP_PATH
ISO_DAY = _rules.ISO_DAY
MONTHS = _rules.MONTHS


def _canonical_id(value) -> str:
    return _rules._canonical_id(value, _id=lambda *args, **kwargs: _id(*args, **kwargs))


def _proof_id(value) -> str:
    return _rules._proof_id(
        value, _canonical_id=lambda *args, **kwargs: _canonical_id(*args, **kwargs)
    )


def _store_url(value: str) -> tuple[str, str] | None:
    return _rules._store_url(
        value,
        _canonical_id=lambda *args, **kwargs: _canonical_id(*args, **kwargs),
        STORE_HOST=STORE_HOST,
        APP_PATH=APP_PATH,
        urlsplit=lambda *args, **kwargs: urlsplit(*args, **kwargs),
    )


def _store_day(raw: str) -> str | None:
    return _rules._store_day(raw, ISO_DAY=ISO_DAY, MONTHS=MONTHS, date=date, re=re)


def _descriptor_ids(value) -> list[int]:
    return _rules._descriptor_ids(
        value, deepcopy=lambda *args, **kwargs: deepcopy(*args, **kwargs)
    )


def normalize_website_identity(value: dict, twitch_id, igdb_id) -> dict:
    return _rules.normalize_website_identity(
        value,
        twitch_id,
        igdb_id,
        _canonical_id=lambda *args, **kwargs: _canonical_id(*args, **kwargs),
        _proof_id=lambda *args, **kwargs: _proof_id(*args, **kwargs),
        deepcopy=lambda *args, **kwargs: deepcopy(*args, **kwargs),
        METHOD=METHOD,
        PROVIDER=PROVIDER,
        STORE_HOST=STORE_HOST,
        parse_timestamp=lambda *args, **kwargs: parse_timestamp(*args, **kwargs),
        _store_url=lambda *args, **kwargs: _store_url(*args, **kwargs),
        _descriptor_ids=lambda *args, **kwargs: _descriptor_ids(*args, **kwargs),
        _store_day=lambda *args, **kwargs: _store_day(*args, **kwargs),
    )


def _steam_metadata(
    appid: str,
    checked_at: str,
    *,
    deadline: float | None,
    monotonic: Callable[[], float],
) -> dict:
    return _transport.get_steam_identity_metadata(
        appid,
        checked_at,
        deadline=deadline,
        monotonic=monotonic,
        check_deadline=lambda *args: _deadline(*args),
        timeout_type=lambda **kwargs: Timeout(**kwargs),
        request_get=lambda *args, **kwargs: requests.get(*args, **kwargs),
        appdetails_url=lambda: APPDETAILS_URL,
        payload_metadata=lambda payload, current_appid, at: _rules._steam_metadata_from_payload(
            payload,
            current_appid,
            at,
            _canonical_id=lambda value: _canonical_id(value),
            _descriptor_ids=lambda value: _descriptor_ids(value),
            _store_day=lambda value: _store_day(value),
            STORE_HOST=STORE_HOST,
            PROVIDER=PROVIDER,
        ),
    )


def lookup_website_identity(
    client,
    twitch_id,
    igdb_id,
    now: datetime,
    *,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict | None:
    return _application.lookup_website_identity(
        client,
        twitch_id,
        igdb_id,
        now,
        deadline=deadline,
        monotonic=monotonic,
        _canonical_id=lambda value: _canonical_id(value),
        timestamp=lambda value: timestamp(value),
        _now=lambda value: _now(value),
        _deadline=lambda *args: _deadline(*args),
        _pages=lambda *args: _pages(*args),
        _store_url=lambda value: _store_url(value),
        _steam_metadata=lambda *args, **kwargs: _steam_metadata(*args, **kwargs),
        normalize_website_identity=lambda *args: normalize_website_identity(*args),
        method=lambda: METHOD,
    )
