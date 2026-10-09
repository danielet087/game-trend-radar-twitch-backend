"""Read declared release dates without fetching any remote source."""

from __future__ import annotations

import json
from pathlib import Path
from radar_backend.domain.twitch_newness import validate_release_dates


def load_release_dates(
    path: str | Path, *, validate=validate_release_dates, path_type=Path, json_module=json
) -> dict:
    payload = json_module.loads(path_type(path).read_text(encoding="utf-8"))
    return validate(payload)
