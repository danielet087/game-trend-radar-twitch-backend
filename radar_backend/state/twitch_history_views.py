"""Small public views derived from the raw Twitch daily history.

Raw day files under data/twitch_history/ remain the authority. Two views let
pages download only what they display:

* data/twitch_history_compact/<day>.json keeps every hour with just the fields
  the 24-hour chart reads, in the raw file's shape. Only the newest
  COMPACT_DAYS Taipei days are kept, because the chart never reads further back.
* data/twitch_history_close/<day>.json keeps the last usable hour of each day:
  the census the stream-planning page replays for a past date.

A view is merged, never rebuilt from a smaller day file, so a day file that was
recreated with fewer hours cannot erase an hour a view already holds.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path, PurePosixPath
import re

HISTORY_PATH = "data/twitch_history/"
COMPACT_PATH = "data/twitch_history_compact/"
CLOSE_PATH = "data/twitch_history_close/"
VIEW_PATHS = (COMPACT_PATH, CLOSE_PATH)
COMPACT_DAYS = 3
COMPACT_GAME_FIELDS = (
    "game_id", "viewer_count", "streamer_count", "median_viewer_count",
    "filtered_audience", "audience_regions", "observation_status", "observation_freshness",
)
TAIPEI = timezone(timedelta(hours=8))
HOUR_MS = 3_600_000
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
# Same acceptance as timestamp() in src/features/twitch/metrics.js.
_ZONED = re.compile(r"T.*(?:Z|[+-]\d{2}:\d{2})\Z")


def _ms(value):
    if not isinstance(value, str) or not _ZONED.search(value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return (parsed - _EPOCH) // timedelta(milliseconds=1)


def _taipei_day(ms):
    return (_EPOCH + timedelta(milliseconds=ms)).astimezone(TAIPEI).date().isoformat()


def _truthy(value):
    """JavaScript truthiness for parsed JSON values."""
    return not (value is None or value is False or value == ""
                or (type(value) in (int, float) and value == 0))


def _day_end(day):
    return _ms(f"{day}T00:00:00+08:00") + 24 * HOUR_MS - 1


def usable_hours(daily, cutoff_ms=None):
    """The page's rules for an hour it will show (validHistoryHours)."""
    for key, entry in daily["hours"].items():
        if not isinstance(entry, dict):
            continue
        at, observed = _ms(key), _ms(entry.get("generated_at"))
        started = _ms(entry.get("collection_started_at")) if _truthy(entry.get("collection_schedule")) else None
        bucket = started if started is not None else observed
        if (at is None or at % HOUR_MS or observed is None
                or (cutoff_ms is not None and observed > cutoff_ms)
                or bucket // HOUR_MS * HOUR_MS != at
                or _taipei_day(bucket) != daily["date"]
                or not isinstance(entry.get("games"), list)):
            continue
        yield at, key, entry


def closing_hour(daily):
    """The hour latestHistorySnapshot() picks for the end of the day."""
    latest = None
    for hour in usable_hours(daily, _day_end(daily["date"])):
        if latest is None or hour[0] > latest[0]:
            latest = hour
    return latest


def _shape(value, day):
    return (isinstance(value, dict) and value.get("schema_version") == 1
            and value.get("timezone") == "Asia/Taipei" and value.get("date") == day
            and isinstance(value.get("hours"), dict))


def _raw_day(path):
    daily = json.loads(path.read_text(encoding="utf-8"))
    if not _shape(daily, path.stem):
        raise ValueError("Unsupported Twitch daily history")
    return daily


def _existing(path):
    """A malformed view is derived data: rebuild it from the raw day."""
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    return value if _shape(value, path.stem) else None


def _write(path, value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    if not path.exists() or path.read_text(encoding="utf-8") != raw:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(raw, encoding="utf-8")


def _view(day, kind, hours):
    return {"schema_version": 1, "date": day, "timezone": "Asia/Taipei", "view": kind, "hours": hours}


def compact_hour(entry):
    view = {key: entry[key] for key in ("generated_at", "collection_started_at", "collection_schedule")
            if key in entry}
    view["games"] = [{key: game[key] for key in COMPACT_GAME_FIELDS if key in game}
                     for game in entry["games"] if isinstance(game, dict)]
    return view


def compact_view(existing, daily):
    hours = dict(existing["hours"]) if existing else {}
    hours.update({key: compact_hour(entry) for key, entry in daily["hours"].items()
                  if isinstance(entry, dict) and isinstance(entry.get("games"), list)})
    return _view(daily["date"], "compact", hours)


def close_view(existing, daily):
    candidate = closing_hour(daily)
    held = closing_hour(existing) if existing else None
    chosen = held if candidate is None or (held is not None and held[0] > candidate[0]) else candidate
    if chosen is None:
        return None
    _, key, entry = chosen
    return _view(daily["date"], "close", {key: entry})


def _touched_day(day_path):
    path = PurePosixPath(day_path)
    if path.parent.as_posix() + "/" != HISTORY_PATH or path.suffix != ".json" or not _DAY.fullmatch(path.stem):
        raise ValueError("History views require a raw Twitch day path")
    return path.stem


def project_history_views(frontend: Path, day_path: str | None = None) -> None:
    """Update the views of one changed day, or of every raw day when backfilling."""
    raw_root = frontend / HISTORY_PATH
    days = sorted(path.stem for path in raw_root.glob("*.json") if _DAY.fullmatch(path.stem))
    if not days:
        return
    first_compact = (date.fromisoformat(days[-1]) - timedelta(days=COMPACT_DAYS - 1)).isoformat()
    touched = days if day_path is None else [_touched_day(day_path)]
    compact_root, close_root = frontend / COMPACT_PATH, frontend / CLOSE_PATH
    for day in touched:
        daily = _raw_day(raw_root / f"{day}.json")
        closing = close_view(_existing(close_root / f"{day}.json"), daily)
        if closing is not None:
            _write(close_root / f"{day}.json", closing)
        if day >= first_compact:
            _write(compact_root / f"{day}.json", compact_view(_existing(compact_root / f"{day}.json"), daily))
    # A new window may start with a day that has no compact file yet.
    for day in days:
        target = compact_root / f"{day}.json"
        if day >= first_compact and not target.exists():
            _write(target, compact_view(None, _raw_day(raw_root / f"{day}.json")))
    if compact_root.exists():
        for path in compact_root.glob("*.json"):
            if _DAY.fullmatch(path.stem) and path.stem < first_compact:
                path.unlink()
