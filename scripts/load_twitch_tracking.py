"""Load the durable registry from immutable frontend HEAD before collection.

A missing file, failed request or invalid document is a collection failure. It
must never silently turn an established observation list into an empty list.
"""

from __future__ import annotations

# Support both the historical module command and an absolute script path.
if __package__ in {None, ""}:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
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
    from radar_backend.adapters.steam_twitch_mapping import normalize_steam_catalog

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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download consistent Twitch tracking and Steam collection inputs"
    )
    parser.add_argument("--output", default="output/twitch_tracking_input.json")
    parser.add_argument("--steam-output", default="output/steam_catalog_input.json")
    parser.add_argument("--mapping-output", default="output/twitch_steam_mapping_input.json")
    parser.add_argument("--discovery-output", default="output/twitch_steam_discovery_input.json")
    args = parser.parse_args()
    bundle = load_published_inputs()
    write_json(bundle["tracking_state"], args.output)
    write_json(bundle["steam_catalog"], args.steam_output)
    write_json(bundle["steam_mapping_state"], args.mapping_output)
    write_json(bundle["steam_discovery_state"], args.discovery_output)
    print(
        f"Loaded frontend {bundle['source_commit']}: "
        f"{len(bundle['tracking_state']['games'])} tracking entries, "
        f"{len(bundle['steam_catalog']['games'])} Steam games and "
        f"{len(bundle['steam_mapping_state']['games'])} mappings and "
        f"{len(bundle['steam_discovery_state']['games'])} Steam intake discoveries"
    )


if __name__ == "__main__":
    main()
