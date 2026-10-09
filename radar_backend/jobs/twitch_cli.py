"""Compatibility-preserving argument parser for the Twitch collection job."""
from __future__ import annotations

import argparse
from radar_backend.adapters.twitch_candidates import REGISTRY_PATH
from radar_backend.adapters.twitch_newness import RELEASE_DATES_PATH
from radar_backend.adapters.twitch_audience import CACHE_PATH
from radar_backend.domain.time import validate_slot


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect independent Twitch new-game discoveries and matched Steam recent releases by Twitch ID.")
    parser.add_argument("--output", default="output/twitch_live.json")
    parser.add_argument("--min-viewers", type=int, default=7000)
    parser.add_argument("--max-category-pages", type=int, default=5)
    parser.add_argument("--max-stream-pages", type=int, default=150)
    parser.add_argument("--max-api-calls", type=int, default=1200)
    parser.add_argument("--max-collection-seconds", type=float, default=1500,
                        help="Shared census + metadata + follower soft deadline; reserves time for publishing")
    parser.add_argument("--verification-registry", default=str(REGISTRY_PATH))
    parser.add_argument("--release-dates", default=str(RELEASE_DATES_PATH), help="Dated Twitch originalReleaseDate export for the 14-day trial")
    parser.add_argument("--no-release-hints", action="store_true", help="Disable IGDB metadata and its 30-day collection filter")
    parser.add_argument("--followers-cache", default=str(CACHE_PATH), help="Runner-local daily follower-total cache")
    parser.add_argument("--tracking-state", help="Persisted frontend tracking registry; required for scheduled collection")
    parser.add_argument("--steam-catalog", help="Curated Steam catalog from the same frontend commit as the tracking registry")
    parser.add_argument("--steam-mapping", help="Persisted Steam AppID / IGDB / Twitch ID mapping from that frontend commit")
    parser.add_argument("--steam-discovery", help="Persisted Twitch-to-Steam intake discoveries from that frontend commit")
    parser.add_argument("--followers-max-calls", type=int, default=None,
                        help="Optional diagnostic follower request cap; default: no separate cap")
    parser.add_argument("--followers-max-seconds", type=float, default=None,
                        help="Optional diagnostic follower time cap; default: collection deadline only")
    parser.add_argument("--no-filtered-audience", action="store_true", help="Disable follower-qualified statistics (CLI default: enabled)")
    parser.add_argument("--target-slot", type=validate_slot, default=None,
                        help="Requested UTC hour; actual sampling timestamps remain separate")
    parser.add_argument("--trigger-source", choices=("manual", "schedule", "cloudflare"), default="manual")
    return parser

