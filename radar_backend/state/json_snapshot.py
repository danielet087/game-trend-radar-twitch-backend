"""Local JSON snapshot storage; a write is not frontend publication."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def write_json(payload: dict[str, Any], output_path: str | Path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path

