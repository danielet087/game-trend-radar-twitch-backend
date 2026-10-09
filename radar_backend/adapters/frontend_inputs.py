"""Compose durable inputs from a single immutable frontend revision."""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from radar_backend.application import frontend_inputs as application
from radar_backend.adapters import frontend_input_http as transport

from radar_backend.state.json_snapshot import write_json
from radar_backend.state.validation import (
    validate_persisted_discovery,
    validate_persisted_mapping,
    validate_persisted_tracking,
)
from radar_backend.domain.time import parse_time

FRONTEND = "danielet087/game-trend-radar"

TRACKING_PATH = "data/twitch_tracking.json"
STEAM_PATH = "data/steam_upcoming.json"
MAPPING_PATH = "data/twitch_steam_mapping.json"
DISCOVERY_PATH = "data/twitch_steam_discovery.json"


def frontend_head() -> str:
    return transport.frontend_head(frontend=FRONTEND, run=subprocess.run, fullmatch=re.fullmatch)


def read_published_json(commit: str, path: str):
    return transport.read_published_json(
        commit,
        path,
        frontend=FRONTEND,
        request_type=Request,
        open_url=urlopen,
        json_module=json,
    )


def load_published_tracking() -> dict:
    return application.load_published_tracking(
        frontend_head=frontend_head,
        read_published_json=read_published_json,
        validate_persisted_tracking=validate_persisted_tracking,
        tracking_path=TRACKING_PATH,
    )


def load_published_inputs() -> dict:
    from collectors.steam_twitch_mapping import normalize_steam_catalog

    return application.load_published_inputs(
        frontend_head=frontend_head,
        read_published_json=read_published_json,
        normalize_steam_catalog=normalize_steam_catalog,
        clock=lambda: datetime.now(timezone.utc),
        validate_persisted_tracking=validate_persisted_tracking,
        validate_persisted_mapping=validate_persisted_mapping,
        validate_persisted_discovery=validate_persisted_discovery,
        http_error=HTTPError,
        tracking_path=TRACKING_PATH,
        steam_path=STEAM_PATH,
        mapping_path=MAPPING_PATH,
        discovery_path=DISCOVERY_PATH,
    )
