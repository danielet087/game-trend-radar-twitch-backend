"""Load and validate local collection inputs; malformed state never resets."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Callable

from radar_backend.adapters.steam_twitch_mapping import normalize_steam_catalog
from radar_backend.domain.collection import CollectionInputs, CollectionRequest
from radar_backend.state.validation import (
    validate_persisted_discovery, validate_persisted_mapping, validate_persisted_tracking,
)


class JsonCollectionInputStore:
    def __init__(self, *, clock: Callable[[], datetime] | None = None):
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _read(path: str) -> dict:
        return json.loads(Path(path).read_text(encoding="utf-8"))

    def load(self, request: CollectionRequest) -> CollectionInputs:
        tracking = (validate_persisted_tracking(self._read(request.tracking_state))
                    if request.tracking_state else None)
        catalog = self._read(request.steam_catalog) if request.steam_catalog else None
        if catalog is not None:
            normalize_steam_catalog(catalog, self.clock())
        mapping = (validate_persisted_mapping(self._read(request.steam_mapping))
                   if request.steam_mapping else None)
        discovery = (validate_persisted_discovery(self._read(request.steam_discovery))
                     if request.steam_discovery else None)
        return CollectionInputs(tracking, catalog, mapping, discovery)
