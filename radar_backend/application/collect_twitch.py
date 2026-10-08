"""Collect observations using injected inputs and the existing feature collector."""
from __future__ import annotations

from typing import Callable, Protocol

from radar_backend.domain.collection import CollectionInputs, CollectionRequest, TwitchCredentials


class CollectionInputStore(Protocol):
    def load(self, request: CollectionRequest) -> CollectionInputs: ...


def collect_observations(
    request: CollectionRequest,
    credentials: TwitchCredentials,
    *,
    input_store: CollectionInputStore,
    collector: Callable[..., dict],
    run_id: str = "",
    run_attempt: str = "",
) -> dict:
    """Read all inputs before collection; leave storage/publication to the caller."""
    inputs = input_store.load(request)
    payload = collector(
        client_id=credentials.client_id,
        client_secret=credentials.client_secret,
        min_viewers=request.min_viewers,
        max_category_pages=request.max_category_pages,
        max_stream_pages=request.max_stream_pages,
        max_api_calls=request.max_api_calls,
        max_collection_seconds=request.max_collection_seconds,
        registry_path=request.verification_registry,
        release_dates_path=request.release_dates,
        include_release_hints=not request.no_release_hints,
        include_filtered_audience=not request.no_filtered_audience,
        followers_cache_path=request.followers_cache,
        followers_max_calls=request.followers_max_calls,
        followers_max_seconds=request.followers_max_seconds,
        tracking_state=inputs.tracking_state,
        steam_catalog=inputs.steam_catalog,
        steam_mapping_state=inputs.steam_mapping_state,
        steam_discovery_state=inputs.steam_discovery_state,
    )
    if request.target_slot:
        payload["collection_schedule"] = {
            "target_slot": request.target_slot,
            "trigger_source": request.trigger_source,
            "run_id": run_id,
            "run_attempt": run_attempt,
        }
    return payload
