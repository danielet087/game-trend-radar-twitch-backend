"""Read a complete Twitch input bundle from one authoritative DB snapshot."""
from datetime import datetime, timezone
import json

from radar_core.storage import primary
from radar_core.storage.sql import StorageError
from radar_backend.state.validation import (
    validate_persisted_discovery, validate_persisted_mapping, validate_persisted_tracking,
)

SOURCE = "danielet087/game-trend-radar@main"
TRACKING = "data/twitch_tracking.json"
CATALOG = "data/steam_upcoming.json"
MAPPING = "data/twitch_steam_mapping.json"
DISCOVERY = "data/twitch_steam_discovery.json"


def load_inputs():
    from radar_backend.adapters.steam_twitch_mapping import normalize_steam_catalog

    head, files = primary.read_files(SOURCE, [TRACKING, CATALOG, MAPPING, DISCOVERY])
    try:
        documents = {path: json.loads(files[path]) for path in (TRACKING, CATALOG, MAPPING, DISCOVERY)}
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise StorageError("INVALID_TWITCH_DATABASE_INPUT", "A required Twitch database input is missing or invalid.") from None
    tracking = validate_persisted_tracking(documents[TRACKING])
    catalog = documents[CATALOG]
    normalize_steam_catalog(catalog, datetime.now(timezone.utc))
    proof = {"provider": "turso", "source_id": SOURCE,
             **{key: head[key] for key in ("revision", "manifest_digest", "generation")}}
    catalog = {**catalog, "_source_snapshot": proof}
    catalog.pop("_source_commit", None)
    return {"source_commit": None, "source_snapshot": proof, "tracking_state": tracking,
            "steam_catalog": catalog,
            "steam_mapping_state": validate_persisted_mapping(documents[MAPPING]),
            "steam_discovery_state": validate_persisted_discovery(documents[DISCOVERY])}


def load_tracking():
    return validate_persisted_tracking(primary.read_json(SOURCE, TRACKING))
