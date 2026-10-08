"""Strict persisted timestamps without storage or HTTP dependencies."""
from datetime import datetime, timezone
import re


def parse_time(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Timestamp must be a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)



def hour_slot(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat().replace("+00:00", "Z")



def validate_slot(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:00:00Z", value) or hour_slot(parse_time(value)) != value:
        raise ValueError("target_slot must be a UTC hour, for example 2026-09-30T00:00:00Z")
    return value

