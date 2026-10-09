"""Load one immutable frontend revision through the canonical input composition."""

from __future__ import annotations

import argparse

from radar_backend.adapters.frontend_inputs import load_published_inputs
from radar_backend.state.json_snapshot import write_json


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
