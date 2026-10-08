"""Collection request and input requirements without IO or CLI dependencies."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CollectionRequest:
    output: str
    min_viewers: int
    max_category_pages: int
    max_stream_pages: int
    max_api_calls: int
    max_collection_seconds: float
    verification_registry: str
    release_dates: str
    no_release_hints: bool
    followers_cache: str
    tracking_state: str | None
    steam_catalog: str | None
    steam_mapping: str | None
    steam_discovery: str | None
    followers_max_calls: int | None
    followers_max_seconds: float | None
    no_filtered_audience: bool
    target_slot: str | None
    trigger_source: str


@dataclass(frozen=True)
class TwitchCredentials:
    client_id: str
    client_secret: str = field(repr=False)


@dataclass(frozen=True)
class CollectionInputs:
    tracking_state: dict | None = None
    steam_catalog: dict | None = None
    steam_mapping_state: dict | None = None
    steam_discovery_state: dict | None = None


def validate_input_requirements(request: CollectionRequest, *, production: bool) -> None:
    """A production run must preserve all enrollment and Steam intake inputs."""
    if not request.tracking_state and production:
        raise ValueError("--tracking-state is required for production collection; refusing to reset enrollments")
    if production and (not request.steam_catalog or not request.steam_mapping):
        raise ValueError("--steam-catalog and --steam-mapping are required for production collection")
    if production and not request.steam_discovery:
        raise ValueError("--steam-discovery is required for production collection; refusing to reset Steam intake")
    if request.steam_mapping and not request.steam_catalog:
        raise ValueError("--steam-mapping requires --steam-catalog")
    if request.steam_discovery and not request.steam_catalog:
        raise ValueError("--steam-discovery requires --steam-catalog")
