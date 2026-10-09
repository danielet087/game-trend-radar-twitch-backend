"""Canonical Twitch census CLI with validated inputs and a local snapshot output."""

from __future__ import annotations

import os

from radar_backend.adapters.twitch_candidates import collect_candidates as collect_twitch
from radar_backend.domain.collection import CollectionRequest
from radar_backend.jobs.twitch import run_collection_job
from radar_backend.jobs.twitch_cli import build_parser
from radar_backend.state.json_snapshot import write_json


def main() -> None:
    args = build_parser().parse_args()
    # Resolve these names at call time so existing monkeypatches/integrations
    # continue to replace the actual collector and local snapshot writer.
    run_collection_job(
        CollectionRequest(**vars(args)),
        os.environ,
        collector=collect_twitch,
        writer=write_json,
    )


if __name__ == "__main__":
    main()
