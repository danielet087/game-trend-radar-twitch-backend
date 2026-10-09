"""Canonical CLI for merging a complete Twitch snapshot into frontend history."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from radar_backend.adapters.twitch_snapshot import store_snapshot


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("snapshot")
    parser.add_argument("frontend")
    args = parser.parse_args()
    payload = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
    print(store_snapshot(payload, Path(args.frontend)))


if __name__ == "__main__":
    main()
