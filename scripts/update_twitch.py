"""Historical CLI; compose dependencies without duplicating collection policy."""
from __future__ import annotations

# Support both the historical module command and an absolute script path.
if __package__ in {None, ""}:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import os

from radar_backend.state.json_snapshot import write_json
from radar_backend.adapters.twitch_candidates import REGISTRY_PATH, collect_candidates as collect_twitch
from radar_backend.adapters.twitch_newness import RELEASE_DATES_PATH
from radar_backend.adapters.twitch_audience import CACHE_PATH
from radar_backend.adapters.steam_twitch_mapping import normalize_steam_catalog
from radar_backend.domain.collection import CollectionRequest
from radar_backend.domain.time import validate_slot
from radar_backend.jobs.twitch import run_collection_job
from radar_backend.jobs.twitch_cli import build_parser
from radar_backend.state.validation import (
    validate_persisted_discovery, validate_persisted_mapping, validate_persisted_tracking,
)


def main() -> None:
    args = build_parser().parse_args()
    # Resolve these names at call time so existing monkeypatches/integrations
    # continue to replace the actual collector and local snapshot writer.
    run_collection_job(
        CollectionRequest(**vars(args)), os.environ,
        collector=collect_twitch, writer=write_json,
    )


if __name__ == "__main__":
    main()
