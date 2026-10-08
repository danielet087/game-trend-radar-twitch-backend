"""Run a collection job; process environment and local output belong here."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Mapping

from radar_backend.application.collect_twitch import CollectionInputStore, collect_observations
from radar_backend.domain.collection import CollectionRequest, TwitchCredentials, validate_input_requirements
from radar_backend.jobs.twitch_report import report_collection
from radar_backend.state.json_inputs import JsonCollectionInputStore


def run_collection_job(
    request: CollectionRequest,
    environment: Mapping[str, str],
    *,
    collector: Callable[..., dict],
    writer: Callable[[dict, str], Path],
    input_store: CollectionInputStore | None = None,
    reporter: Callable[..., None] = report_collection,
) -> Path:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    client_id = environment.get("TWITCH_CLIENT_ID", "").strip()
    client_secret = environment.get("TWITCH_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise SystemExit("TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET are required")
    production = request.trigger_source != "manual" or bool(environment.get("GITHUB_RUN_ID"))
    try:
        validate_input_requirements(request, production=production)
    except ValueError as error:
        raise SystemExit(str(error)) from None
    payload = collect_observations(
        request, TwitchCredentials(client_id, client_secret),
        input_store=input_store if input_store is not None else JsonCollectionInputStore(),
        collector=collector,
        run_id=environment.get("GITHUB_RUN_ID", ""),
        run_attempt=environment.get("GITHUB_RUN_ATTEMPT", ""),
    )
    path = writer(payload, request.output)
    reporter(payload, request, path, summary_path=environment.get("GITHUB_STEP_SUMMARY"))
    return path
