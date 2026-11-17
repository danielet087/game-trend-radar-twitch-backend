"""Local JSON snapshot storage; a write is not frontend publication."""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

# Rewritten every hour and read only by programs: without indentation they are
# about a third smaller, both as files and after gzip. Other files stay
# indented so their Git diffs remain readable.
COMPACT_FILES = frozenset({"twitch_live.json", "twitch_tracking.json", "twitch_steam_mapping.json"})
# Raw day files (data/twitch_history/YYYY-MM-DD.json) live only in the database
# and the monthly release archives; compact they are about 45% smaller. Only
# the current day is ever rewritten, and the archive re-packs a day whose bytes
# changed as long as it keeps every archived hour.
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


def compact_path(path: str | Path) -> bool:
    path = Path(path)
    return path.name in COMPACT_FILES or (path.parent.name == "twitch_history" and _DAY.fullmatch(path.stem) is not None
                                          and path.suffix == ".json")


def render_json(payload: Any, path: str | Path | None = None, *, allow_nan: bool = True) -> str:
    if path is not None and compact_path(path):
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=allow_nan) + "\n"
    return json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=allow_nan) + "\n"


def write_json(payload: dict[str, Any], output_path: str | Path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_json(payload, path), encoding="utf-8")
    return path
