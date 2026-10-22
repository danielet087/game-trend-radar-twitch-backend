"""Small, lossless metric projections for one game's monthly history.

The coverage index distinguishes an uncollected hour from a collected census
that omitted a game. Raw daily snapshots remain the authoritative evidence.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re

TAIPEI = timezone(timedelta(hours=8))
SERIES_PATH = "data/twitch_series/"


def _time(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else None
    except ValueError:
        return None


def _write(path, value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    if not path.exists() or path.read_text(encoding="utf-8") != raw:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(raw, encoding="utf-8")


def project_series(frontend: Path, day_path: str | None = None) -> None:
    """Backfill once; subsequently reproject only the changed census day.

Replacing every row for each affected hour makes retries and corrections
idempotent, including a category removed from a replacement census.
"""
    root = frontend / SERIES_PATH
    index_path = root / "index.json"
    index = json.loads(index_path.read_text()) if index_path.exists() else {
        "schema_version": 1, "timezone": "Asia/Taipei", "months": {}, "games": {},
    }
    if index.get("schema_version") != 1 or index.get("timezone") != "Asia/Taipei":
        raise ValueError("Unsupported Twitch series index")
    days = ([frontend / day_path] if day_path and index_path.exists() else
            sorted((frontend / "data/twitch_history").glob("????-??-??.json")))
    updates = defaultdict(dict)
    metadata = {}
    for path in days:
        daily = json.loads(path.read_text())
        day = daily.get("date", "")
        if daily.get("schema_version") != 1 or daily.get("timezone") != "Asia/Taipei" or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise ValueError("Unsupported Twitch daily history")
        for hour, sample in daily.get("hours", {}).items():
            at, generated = _time(hour), _time(sample.get("generated_at"))
            bucket = _time(sample.get("collection_started_at") if sample.get("collection_schedule") else sample.get("generated_at"))
            if (not at or not generated or not bucket or at.minute or at.second or at.microsecond
                    or bucket.replace(minute=0, second=0, microsecond=0) != at
                    or bucket.astimezone(TAIPEI).date().isoformat() != day
                    or not isinstance(sample.get("games"), list)):
                continue
            # Use canonical UTC keys in both the coverage index and game chunks.
            key = at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            finished = generated.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            month = day[:7]
            index["months"].setdefault(month, {})[key] = finished
            rows = {}
            for game in sample["games"]:
                game_id = str(game.get("game_id", ""))
                if (not re.fullmatch(r"[1-9]\d{0,15}", game_id)
                        or game.get("observation_status") in {"retained", "stale"}
                        or game.get("observation_freshness") == "stale"):
                    continue
                rows[game_id] = {"generated_at": finished, **{
                    name: game[name] for name in ("viewer_count", "streamer_count", "filtered_audience") if name in game
                }}
                if game_id not in metadata or key >= metadata[game_id][0]:
                    metadata[game_id] = (key, str(game.get("game_name") or game_id), game.get("box_art_url"))
            updates[month][key] = rows
    for month, hours in updates.items():
        game_ids = {game_id for rows in hours.values() for game_id in rows}
        game_ids.update(game_id for game_id, game in index["games"].items() if month in game["months"])
        for game_id in sorted(game_ids):
            path = root / game_id / f"{month}.json"
            series = json.loads(path.read_text()) if path.exists() else {
                "schema_version": 1, "timezone": "Asia/Taipei", "game_id": game_id, "month": month, "hours": {},
            }
            for hour, rows in hours.items():
                series["hours"].pop(hour, None)
                if game_id in rows:
                    series["hours"][hour] = rows[game_id]
            _write(path, series)
            game = index["games"].setdefault(game_id, {"name": game_id, "months": []})
            game["months"] = sorted(set(game["months"]) | {month})
            available = list(series["hours"])
            for other_month in game["months"]:
                if other_month != month:
                    other = json.loads((root / game_id / f"{other_month}.json").read_text())
                    available.extend(other["hours"])
            game["first_hour"] = min(available, default=None)
            game["last_hour"] = max(available, default=None)
            if game_id in metadata and metadata[game_id][0] >= game.get("name_at", ""):
                game["name_at"], game["name"], game["box_art_url"] = metadata[game_id]
    index["generated_at"] = max((at for hours in index["months"].values() for at in hours.values()), default=None)
    _write(index_path, index)
