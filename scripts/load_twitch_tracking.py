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

from collectors.twitch_live import write_json
from radar_backend.state.validation import (
    validate_persisted_discovery, validate_persisted_mapping, validate_persisted_tracking,
)
from scripts.collection_guard import FRONTEND
from scripts.collection_guard import parse_time

TRACKING_PATH = "data/twitch_tracking.json"
STEAM_PATH = "data/steam_upcoming.json"
MAPPING_PATH = "data/twitch_steam_mapping.json"
DISCOVERY_PATH = "data/twitch_steam_discovery.json"


def frontend_head() -> str:
    remote = subprocess.run(
        ["git", "ls-remote", f"https://github.com/{FRONTEND}.git", "refs/heads/main"],
        check=True, capture_output=True, text=True, timeout=30,
    ).stdout.strip().split()
    if len(remote) != 2 or not re.fullmatch(r"[0-9a-f]{40}", remote[0]) or remote[1] != "refs/heads/main":
        raise ValueError("Cannot resolve current frontend HEAD for tracking state")
    return remote[0]


def read_published_json(commit: str, path: str):
    url = f"https://raw.githubusercontent.com/{FRONTEND}/{commit}/{path}"
    request = Request(url, headers={"User-Agent": "game-trend-radar-tracking-loader"})
    with urlopen(request, timeout=20) as response:
        payload = json.load(response)
    return payload


def load_published_tracking() -> dict:
    """Compatibility helper for registry-only diagnostics."""
    return validate_persisted_tracking(read_published_json(frontend_head(), TRACKING_PATH))


def load_published_inputs() -> dict:
    """Resolve one frontend revision, then load its complete collection inputs.

    Only absent mapping/discovery files are supported bootstrap conditions. Missing
    required data, invalid JSON and any non-404 HTTP error stop collection.
    """
    from collectors.steam_twitch_mapping import normalize_steam_catalog

    commit = frontend_head()
    tracking = validate_persisted_tracking(read_published_json(commit, TRACKING_PATH))
    catalog = read_published_json(commit, STEAM_PATH)
    normalize_steam_catalog(catalog, datetime.now(timezone.utc))
    catalog = {**catalog, "_source_commit": commit}
    try:
        mapping = read_published_json(commit, MAPPING_PATH)
    except HTTPError as error:
        if error.code != 404:
            raise
        mapping = {"schema_version": 1, "updated_at": None, "games": {}}
    mapping = validate_persisted_mapping(mapping)
    try:
        discovery = read_published_json(commit, DISCOVERY_PATH)
    except HTTPError as error:
        if error.code != 404:
            raise
        discovery = {"schema_version": 1, "updated_at": None, "games": {}}
    discovery = validate_persisted_discovery(discovery)
    return {"source_commit": commit, "tracking_state": tracking,
            "steam_catalog": catalog, "steam_mapping_state": mapping,
            "steam_discovery_state": discovery}


def main() -> None:
    parser = argparse.ArgumentParser(description="Download consistent Twitch tracking and Steam collection inputs")
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
    print(f"Loaded frontend {bundle['source_commit']}: "
          f"{len(bundle['tracking_state']['games'])} tracking entries, "
          f"{len(bundle['steam_catalog']['games'])} Steam games and "
          f"{len(bundle['steam_mapping_state']['games'])} mappings and "
          f"{len(bundle['steam_discovery_state']['games'])} Steam intake discoveries")


if __name__ == "__main__":
    main()
