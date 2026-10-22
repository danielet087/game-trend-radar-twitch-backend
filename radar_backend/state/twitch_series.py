"""Small, lossless metric projections for one game's monthly history.

The coverage index distinguishes an uncollected hour from a collected census
that omitted a game. Raw daily snapshots remain the authoritative evidence.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re
from urllib.parse import urlsplit

from radar_backend.domain.twitch_archive import qualified_archive_sources

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


def cover_url(value):
    if not isinstance(value, str):
        return None
    try:
        url = urlsplit(value)
        return value if url.scheme == "https" and url.netloc == "static-cdn.jtvnw.net" and url.path.startswith("/ttv-boxart/") else None
    except ValueError:
        return None


def _calendar_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return value if date.fromisoformat(value).isoformat() == value else None
    except ValueError:
        return None


def _release_reference(entry, matched_steam, admitted_sources):
    """A Day 0 reference describes a verified date, not the tracking window.

    Steam's normalized Taiwan calendar date is authoritative even when its
    timestamp audit records a different day. Different matched editions must
    agree on the date before they can share a Steam reference.
    """
    dates = defaultdict(set)
    for appid, steam in matched_steam:
        released = _calendar_date(steam.get("release_date"))
        if (released and steam.get("release_precision") == "day"
                and steam.get("release_date_timezone") == "Asia/Taipei"):
            dates[released].add(appid)
    if len(dates) == 1:
        released, appids = next(iter(dates.items()))
        reference = {"date": released, "source": "Steam 台灣商店", "precision": "day",
                     "source_type": "steam_taiwan_store_date"}
        if len(appids) == 1:
            reference["steam_appid"] = next(iter(appids))
        return reference

    sources = (entry or {}).get("tracking_sources")
    if sources is None:
        sources = {"twitch_new": entry}
    if not isinstance(sources, dict) or "twitch_new" not in admitted_sources:
        return None
    source = sources.get("twitch_new")
    if not isinstance(source, dict):
        return None
    evidence = source.get("enrollment") or {}
    kind = source.get("release_source") or evidence.get("source")
    labels = {"igdb_first_release_date": "IGDB 首次發售日",
              "twitch_original_release_date": "Twitch 首次發售日"}
    released = _time(source.get("release_at"))
    if kind in labels and released:
        return {"date": released.astimezone(TAIPEI).date().isoformat(),
                "source": labels[kind], "precision": "day", "source_type": kind}
    return None


def _archive_metadata(frontend, root, index):
    """Keep archive discovery independent of the latest live candidate list."""
    def optional(name):
        path = frontend / "data" / name
        return json.loads(path.read_text()) if path.exists() else None

    registry = optional("twitch_tracking.json")
    mapping = optional("twitch_steam_mapping.json")
    tracked = (registry or {}).get("games", {})
    forward = (mapping or {}).get("games", {})
    as_of = max(filter(None, [_time(index.get("generated_at")), _time((registry or {}).get("updated_at"))]), default=None)
    mapped, covers, matched_releases = defaultdict(list), {}, defaultdict(list)
    for appid, entry in forward.items():
        if entry.get("status") == "matched" and cover_url(entry.get("box_art_url")):
            covers[str(entry.get("twitch_game_id"))] = entry["box_art_url"]
        if entry.get("status") == "matched" and isinstance(entry.get("steam"), dict):
            mapped[str(entry.get("twitch_game_id"))].append({**entry["steam"], "steam_appid": appid})
            # Use the same matched identity decision as archive metadata, but
            # never borrow release dates from unverified names or aliases.
            if (re.fullmatch(r"[1-9]\d*", appid)
                    and str(entry.get("steam_appid", appid)) == appid
                    and str(entry["steam"].get("steam_appid", appid)) == appid):
                matched_releases[str(entry.get("twitch_game_id"))].append((appid, entry["steam"]))

    def active(source, steam=False):
        if source.get("status") != "active" or as_of is None:
            return False
        expiry, release = _time(source.get("expires_at")), _time(source.get("release_at"))
        return (not source.get("expires_at") or expiry is not None and expiry > as_of) and (
            not steam or release is not None and expiry is not None and release <= as_of)

    for game_id, game in index["games"].items():
        samples = {}
        for month in game["months"]:
            chunk = json.loads((root / game_id / f"{month}.json").read_text())
            samples.update({hour: row for hour, row in chunk["hours"].items()
                            if row.get("generated_at") == index["months"].get(month, {}).get(hour)})
        viewers = [row.get("viewer_count") for row in samples.values()]
        viewers = [value for value in viewers if type(value) in {int, float} and math.isfinite(value) and value >= 0]
        game.update(first_hour=min(samples, default=None), last_hour=max(samples, default=None),
                    sample_count=len(samples), viewer_sample_count=len(viewers),
                    peak_viewer_count=max(viewers, default=None))
        entry = tracked.get(game_id)
        game["archive_eligible"] = True
        game["admission_sources"] = qualified_archive_sources(game_id, entry)
        release = _release_reference(entry, matched_releases.get(game_id, []), game["admission_sources"])
        if release:
            game["release_reference"] = release
        else:
            game.pop("release_reference", None)
        game["box_art_url"] = cover_url((entry or {}).get("box_art_url")) or covers.get(game_id) or cover_url(game.get("box_art_url"))
        if not entry:
            status = "untracked" if registry is not None else "unknown"
        elif entry.get("status") in {"expired", "excluded"}:
            status = "ended"
        elif entry.get("status") == "active":
            sources = entry.get("tracking_sources")
            is_active = any(active(source, key.startswith("steam:")) for key, source in sources.items()) if isinstance(sources, dict) else active(entry)
            status = "active" if is_active else "ended"
        else:
            status = "unknown"
        game["tracking_status"] = status
        game["tracking_reason"] = (entry or {}).get("status_reason")
        steam = []
        for item in (entry or {}).get("steam_matches", []):
            decision = forward.get(str(item.get("steam_appid")))
            if decision and (decision.get("status") != "matched" or str(decision.get("twitch_game_id")) != game_id):
                continue
            steam.append(item)
        steam = mapped.get(game_id, []) + steam
        names = [game["name"], (entry or {}).get("game_name")]
        for item in steam:
            names.extend(item.get(key) for key in ("display_name", "name", "name_en"))
        game["aliases"] = sorted({name.strip() for name in names if isinstance(name, str) and name.strip()})
        game["display_name"] = next((item["display_name"] for item in steam if isinstance(item.get("display_name"), str) and item["display_name"].strip()), game["name"])
        game["steam_appids"] = sorted({str(item["steam_appid"]) for item in steam if re.fullmatch(r"[1-9]\d*", str(item.get("steam_appid", "")))})
    index["archive_version"] = 2
    index["archive_scope"] = "qualified_tracking_admissions"


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
    tracking_path = frontend / "data/twitch_tracking.json"
    if not tracking_path.exists() and index["games"]:
        raise ValueError("Tracking registry is required to curate existing Twitch history")
    registry = json.loads(tracking_path.read_text()) if tracking_path.exists() else {"schema_version": 1, "games": {}}
    if registry.get("schema_version") != 1 or not isinstance(registry.get("games"), dict):
        raise ValueError("Unsupported Twitch tracking registry")
    admitted = {game_id for game_id, entry in registry["games"].items() if qualified_archive_sources(game_id, entry)}
    # Old raw samples remain auditable, but cannot create an archive membership.
    index["games"] = {game_id: game for game_id, game in index["games"].items() if game_id in admitted}
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
                if (game_id not in admitted
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
            if game_id in metadata and metadata[game_id][0] >= game.get("name_at", ""):
                game["name_at"], game["name"], cover = metadata[game_id]
                if cover_url(cover):
                    game["box_art_url"] = cover
    index["generated_at"] = max((at for hours in index["months"].values() for at in hours.values()), default=None)
    _archive_metadata(frontend, root, index)
    _write(index_path, index)
