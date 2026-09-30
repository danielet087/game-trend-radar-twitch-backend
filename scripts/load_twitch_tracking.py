"""Load the durable registry from immutable frontend HEAD before collection.

A missing file, failed request or invalid document is a collection failure. It
must never silently turn an established observation list into an empty list.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from urllib.request import Request, urlopen

from collectors.twitch_live import write_json
from collectors.twitch_tracking import normalize_tracking_state
from scripts.collection_guard import FRONTEND
from scripts.collection_guard import parse_time

TRACKING_PATH = "data/twitch_tracking.json"


def validate_persisted_tracking(payload: dict) -> dict:
    # None is useful for an isolated collector's first run, never a valid
    # downloaded document. The persisted clock is needed for race-safe merges.
    if not isinstance(payload, dict):
        raise ValueError("Persisted tracking state must be an object")
    parse_time(payload.get("updated_at"))
    return normalize_tracking_state(payload)


def load_published_tracking() -> dict:
    remote = subprocess.run(
        ["git", "ls-remote", f"https://github.com/{FRONTEND}.git", "refs/heads/main"],
        check=True, capture_output=True, text=True, timeout=30,
    ).stdout.strip().split()
    if len(remote) != 2 or not re.fullmatch(r"[0-9a-f]{40}", remote[0]) or remote[1] != "refs/heads/main":
        raise ValueError("Cannot resolve current frontend HEAD for tracking state")
    url = f"https://raw.githubusercontent.com/{FRONTEND}/{remote[0]}/{TRACKING_PATH}"
    request = Request(url, headers={"User-Agent": "game-trend-radar-tracking-loader"})
    with urlopen(request, timeout=20) as response:
        payload = json.load(response)
    return validate_persisted_tracking(payload)


def main() -> None:
    parser = argparse.ArgumentParser(description="Download required persistent Twitch tracking state")
    parser.add_argument("--output", default="output/twitch_tracking_input.json")
    args = parser.parse_args()
    state = load_published_tracking()
    write_json(state, args.output)
    print(f"Loaded {len(state['games'])} persistent tracking entries -> {args.output}")


if __name__ == "__main__":
    main()
