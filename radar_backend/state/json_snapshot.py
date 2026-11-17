"""Local JSON snapshot storage; a write is not frontend publication."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Rewritten every hour and read only by programs: without indentation they are
# about a third smaller, both as files and after gzip. Other files stay
# indented so their Git diffs remain readable.
COMPACT_FILES = frozenset({"twitch_live.json", "twitch_tracking.json", "twitch_steam_mapping.json"})


def render_json(payload: Any, path: str | Path | None = None, *, allow_nan: bool = True) -> str:
    if path is not None and Path(path).name in COMPACT_FILES:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=allow_nan) + "\n"
    return json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=allow_nan) + "\n"


def write_json(payload: dict[str, Any], output_path: str | Path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_json(payload, path), encoding="utf-8")
    return path
