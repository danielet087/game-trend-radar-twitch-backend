"""Verification registry filesystem input."""

from __future__ import annotations
from pathlib import Path
from typing import Any


def load_verifications(
    path: str | Path,
    *,
    path_type,
    json_module,
    validate,
    parse_timestamp,
    timedelta_type,
) -> dict[str, dict[str, Any]]:
    payload = json_module.loads(path_type(path).read_text(encoding="utf-8"))
    return validate(payload, parse_timestamp=parse_timestamp, timedelta_type=timedelta_type)
