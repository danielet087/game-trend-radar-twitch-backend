"""Load the durable registry from immutable frontend HEAD before collection.

A missing file, failed request or invalid document is a collection failure. It
must never silently turn an established observation list into an empty list.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from collectors.twitch_live import write_json
from collectors.twitch_tracking import normalize_tracking_state
from scripts.collection_guard import FRONTEND
from scripts.collection_guard import parse_time

TRACKING_PATH = "data/twitch_tracking.json"
STEAM_PATH = "data/steam_upcoming.json"
MAPPING_PATH = "data/twitch_steam_mapping.json"


def validate_persisted_tracking(payload: dict) -> dict:
    # None is useful for an isolated collector's first run, never a valid
    # downloaded document. The persisted clock is needed for race-safe merges.
    if not isinstance(payload, dict):
        raise ValueError("Persisted tracking state must be an object")
    parse_time(payload.get("updated_at"))
    return normalize_tracking_state(payload)


def validate_persisted_mapping(payload: dict) -> dict:
    from collectors.steam_twitch_mapping import normalize_mapping_state

    if not isinstance(payload, dict) or "updated_at" not in payload:
        raise ValueError("Persisted Steam/Twitch mapping must be a dated object")
    state = normalize_mapping_state(payload)
    if state["games"]:
        parse_time(state.get("updated_at"))
        for entry in state["games"].values():
            if entry.get("status") != "pending" or entry.get("checked_at") is not None:
                parse_time(entry.get("checked_at"))
    return state


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

    Only an absent mapping file is a supported bootstrap condition. Missing
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
    return {"source_commit": commit, "tracking_state": tracking,
            "steam_catalog": catalog, "steam_mapping_state": mapping}


def main() -> None:
    parser = argparse.ArgumentParser(description="Download consistent Twitch tracking and Steam collection inputs")
    parser.add_argument("--output", default="output/twitch_tracking_input.json")
    parser.add_argument("--steam-output", default="output/steam_catalog_input.json")
    parser.add_argument("--mapping-output", default="output/twitch_steam_mapping_input.json")
    args = parser.parse_args()
    bundle = load_published_inputs()
    write_json(bundle["tracking_state"], args.output)
    write_json(bundle["steam_catalog"], args.steam_output)
    write_json(bundle["steam_mapping_state"], args.mapping_output)
    print(f"Loaded frontend {bundle['source_commit']}: "
          f"{len(bundle['tracking_state']['games'])} tracking entries, "
          f"{len(bundle['steam_catalog']['games'])} Steam games and "
          f"{len(bundle['steam_mapping_state']['games'])} mappings")


if __name__ == "__main__":
    main()
