"""Authoritative Steam AppID -> IGDB -> Twitch category links.

Names never establish a link. A missing or failed lookup is not zero viewers.
Only Twitch supplies category artwork; Steam supplies compact store metadata.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import re
import time
from typing import Callable
from zoneinfo import ZoneInfo

import requests
from urllib3.util import Timeout

from radar_backend.domain.twitch import CollectionDeadlineExceeded
from radar_backend.domain.twitch_newness import parse_timestamp, timestamp
from collectors.twitch_steam_admission import (
    TW_STORE_DATE_AUTHORITY, has_taiwan_store_date_authority, is_twitch_qualified,
)

TAIPEI = ZoneInfo("Asia/Taipei")
METHOD = "steam_appid_igdb_external_games_helix_igdb_id"
IGDB_BASE = "https://api.igdb.com/v4/"
RETRY_INTERVAL = timedelta(hours=24)
DAY = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


def _id(value) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("ID must be a positive decimal integer")
    text = str(value)
    if not text.isascii() or not text.isdigit() or int(text) <= 0:
        raise ValueError("ID must be a positive decimal integer")
    return str(int(text))


def _now(now: datetime | None) -> datetime:
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        raise ValueError("Clock must include a timezone")
    return clock.astimezone(timezone.utc)


def normalize_mapping_state(payload: dict | None) -> dict:
    """Validate persisted identity links without reinterpreting their statuses."""
    if payload is None:
        return {"schema_version": 1, "updated_at": None, "games": {}}
    if not isinstance(payload, dict) or payload.get("schema_version") != 1 or not isinstance(payload.get("games"), dict):
        raise ValueError("Invalid Steam/Twitch mapping registry")
    state = deepcopy(payload)
    if state.get("updated_at") is not None:
        parse_timestamp(state["updated_at"])
    if state.get("steam_source_id") is not None:
        state["steam_source_id"] = _id(state["steam_source_id"])
    for appid, row in state["games"].items():
        if not isinstance(appid, str) or _id(appid) != appid or not isinstance(row, dict):
            raise ValueError("Invalid mapping AppID or record")
        if _id(row.get("steam_appid")) != appid or row.get("status") not in {"matched", "unmatched", "ambiguous", "pending"}:
            raise ValueError("Invalid mapping identity or status")
        if not isinstance(row.get("steam"), dict) or _id(row["steam"].get("steam_appid")) != appid:
            raise ValueError("Mapping requires corresponding Steam metadata")
        for key in ("checked_at", "retry_at", "metadata_updated_at"):
            if row.get(key) is not None:
                parse_timestamp(row[key])
        for key in ("igdb_id", "twitch_game_id"):
            if row.get(key) is not None:
                row[key] = _id(row[key])
        if row["status"] == "matched":
            if not row.get("igdb_id") or not row.get("twitch_game_id") or row.get("method") != METHOD:
                raise ValueError("Confirmed mapping requires authoritative IDs and provenance")
    return state


def _strings(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(item.strip() for item in value if isinstance(item, str) and item.strip()))


def _labels(value, allowed: list[str]) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {key: value[key].strip() for key in allowed if isinstance(value.get(key), str) and value[key].strip()}


def normalize_steam_catalog(payload: dict, now: datetime) -> list[dict]:
    """Read the curated public catalog; uncertain/conflicting release rows cannot enroll.

    An exact UTC release instant takes precedence over Taipei midnight. Future
    releases remain useful for identity links, but are never recent releases.
    """
    clock = _now(now)
    if (not isinstance(payload, dict) or payload.get("version") != 2
            or not isinstance(payload.get("games"), list)
            or type(payload.get("count")) is not int or payload["count"] != len(payload["games"])):
        raise ValueError("Invalid curated Steam catalog envelope")
    parse_timestamp(payload.get("generated_at"))
    result, seen = [], set()
    for row in payload["games"]:
        if not isinstance(row, dict):
            raise ValueError("Steam catalog game must be an object")
        appid = _id(row.get("appid"))
        if appid in seen:
            raise ValueError("Duplicate Steam catalog AppID")
        seen.add(appid)
        start, end = row.get("release_start"), row.get("release_end")
        taiwan_authority = has_taiwan_store_date_authority(row) and is_twitch_qualified(row)
        if (row.get("release_precision") != "day" or not isinstance(start, str)
                or not DAY.fullmatch(start) or start != end
                or row.get("release_date_conflict") is True and not taiwan_authority
                or row.get("release_date_timezone", "Asia/Taipei") != "Asia/Taipei"):
            continue
        try:
            release_day = date.fromisoformat(start)
            if row.get("release_time_utc"):
                release = parse_timestamp(row["release_time_utc"])
                if release.astimezone(TAIPEI).date() != release_day and not taiwan_authority:
                    continue
            else:
                release = datetime.combine(release_day, datetime.min.time(), TAIPEI).astimezone(timezone.utc)
            if row.get("release_timestamp_taipei_date", start) != start and not taiwan_authority:
                continue
            if taiwan_authority:
                # The verified TW store day governs the release window. Keep
                # the other official timestamp below as diagnostic evidence.
                release = datetime.combine(release_day, datetime.min.time(), TAIPEI).astimezone(timezone.utc)
        except (ValueError, TypeError, OverflowError):
            continue
        name = row.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Steam catalog game requires a name")
        display = row.get("display_name") or name
        english = row.get("name_en") or name
        followers = row.get("followers")
        if not isinstance(display, str) or not isinstance(english, str) or type(followers) is not int or followers < 0:
            raise ValueError("Invalid Steam catalog display metadata")
        tags, genres = _strings(row.get("tags")), _strings(row.get("genres"))
        expires = release + timedelta(days=30)
        result.append({
            "steam_appid": appid, "name": name.strip(), "display_name": display.strip(), "name_en": english.strip(),
            "store_url": f"https://store.steampowered.com/app/{appid}/", "followers": followers,
            "release_at": timestamp(release), "release_date": start, "release_date_timezone": "Asia/Taipei",
            "release_precision": "day",
            "release_time_basis": TW_STORE_DATE_AUTHORITY if taiwan_authority else (
                "exact_utc" if row.get("release_time_utc") else "taipei_date_midnight"),
            "is_recent": release <= clock < expires, "expires_at": timestamp(expires),
            "tags": tags, "genres": genres,
            "tag_labels_zh_tw": _labels(row.get("tag_labels_zh_tw"), tags),
            "genre_labels_zh_tw": _labels(row.get("genre_labels_zh_tw"), genres),
            **({key: deepcopy(row.get(key)) for key in (
                "release_time_utc", "release_timestamp_taipei_date", "release_date_conflict", "release_store_date",
                "release_date_normalization", "release_display_provider", "release_date_verified_at",
            )} if taiwan_authority else {}),
        })
    return result


def _deadline(deadline: float | None, monotonic: Callable[[], float]) -> None:
    if deadline is not None and monotonic() >= deadline:
        raise CollectionDeadlineExceeded("collection_deadline_exhausted")


def _igdb(client, endpoint: str, query: str, deadline, monotonic) -> list[dict]:
    _deadline(deadline, monotonic)
    if not client.access_token:
        client.authenticate()
    client._wait()
    # The shared collector normally uses 0.3 seconds, but this helper must
    # also respect IGDB's four-per-second limit with a default Twitch client.
    if getattr(client, "request_interval", 0.25) < 0.25:
        delay = 0.25 - (monotonic() - client._last_request_at)
        if delay > 0:
            client._sleep_with_deadline(delay)
    _deadline(deadline, monotonic)
    remaining = deadline - monotonic() if deadline is not None else None
    timeout = (Timeout(total=remaining, connect=min(client.timeout_seconds, remaining), read=min(client.timeout_seconds, remaining))
               if remaining is not None else client.timeout_seconds)
    try:
        response = client.session.post(IGDB_BASE + endpoint, data=query,
            headers={"Client-ID": client.client_id, "Authorization": f"Bearer {client.access_token}"}, timeout=timeout)
    finally:
        client._last_request_at = monotonic()
    _deadline(deadline, monotonic)
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("Invalid IGDB mapping response")
    return rows


def _pages(client, endpoint: str, query: str, deadline, monotonic) -> list[dict]:
    result = []
    # Stable ID ordering avoids treating the default ten-row response as complete.
    for offset in range(0, 50000, 500):
        rows = _igdb(client, endpoint, f"{query} sort id asc; limit 500; offset {offset};", deadline, monotonic)
        if len(rows) > 500:
            raise ValueError("IGDB mapping page exceeded limit")
        result.extend(rows)
        if len(rows) < 500:
            return result
    raise ValueError("IGDB mapping pagination exceeded safe limit")


def _error(exc: Exception) -> str:
    # Never publish exception text: requests can contain credentials or bodies.
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return f"HTTP {status}" if type(status) is int else type(exc).__name__


def _reuse_discovery_mappings(state: dict, catalog: list[dict], discovery_state: dict | None,
                             tracking_state: dict | None, clock: datetime) -> set[str]:
    """Reuse the same validated official ID chain without another API lookup.

    Reverse intake may know a link before the game enters the curated catalog.
    Only its current, qualified Twitch membership and a unique AppID identity
    can seed a forward cache. A conflicting or newer forward decision wins.
    """
    if discovery_state is None or tracking_state is None:
        return set()
    # Local imports avoid the discovery module's shared mapping-helper import.
    from collectors.twitch_steam_discovery import NON_GAME_IDS, _qualified_members, normalize_discovery_state
    from radar_backend.adapters.twitch_tracking import normalize_tracking_state

    discovery = normalize_discovery_state(discovery_state)
    tracking = normalize_tracking_state(tracking_state, clock, non_game_ids=NON_GAME_IDS)
    qualified = _qualified_members(tracking, clock)
    source = discovery.get("steam_source_id")
    if not source or state.get("steam_source_id") not in (None, source):
        return set()
    public_ids = {steam["steam_appid"] for steam in catalog}
    candidates: dict[str, dict[tuple[str, str], tuple[dict, dict]]] = {}
    for twitch_id, row in discovery["games"].items():
        if (twitch_id not in qualified or row.get("active") is not True or row.get("status") != "matched"
                or parse_timestamp(row["checked_at"]) > clock or parse_timestamp(row["updated_at"]) > clock):
            continue
        entry, enrollment = qualified[twitch_id]
        current_igdb = entry.get("igdb_id") or (entry.get("last_observation") or {}).get("igdb_id")
        try:
            same_igdb = _id(current_igdb) == row["igdb_id"]
        except (ValueError, TypeError):
            same_igdb = False
        if not same_igdb or row["twitch_enrollment"] != enrollment:
            continue
        for appid in set(row["steam_appids"]) & public_ids:
            candidates.setdefault(appid, {})[(twitch_id, row["igdb_id"])] = (row, entry)

    reused = set()
    for appid, identities in candidates.items():
        if len(identities) != 1:
            continue
        (twitch_id, igdb_id), (row, entry) = next(iter(identities.items()))
        previous = state["games"].get(appid)
        if previous is not None:
            # Confirmed and ambiguous forward identities require their own
            # authoritative refresh; reverse metadata cannot replace them.
            if previous.get("status") in {"matched", "ambiguous"}:
                continue
            if (previous.get("checked_at") and
                    parse_timestamp(previous["checked_at"]) >= parse_timestamp(row["checked_at"])):
                continue
        cached = state["games"].setdefault(appid, {"steam_appid": appid})
        for key in ("candidates",):
            cached.pop(key, None)
        cached.update(
            status="matched", method=METHOD, igdb_id=igdb_id, twitch_game_id=twitch_id,
            twitch_name=row["twitch_name"], box_art_url=entry.get("box_art_url"),
            checked_at=row["checked_at"], retry_at=None, reason="authoritative_id_chain",
            identity_source=row["method"], discovery_source_updated_at=discovery.get("updated_at"),
            discovery_links=deepcopy([link for link in row["links"] if link["steam_appid"] == appid]),
        )
        reused.add(appid)
    if reused:
        state["steam_source_id"] = source
    return reused


def refresh_mappings(client, catalog: dict | list[dict], mapping_state: dict | None = None,
                     now: datetime | None = None, *, deadline: float | None = None,
                     monotonic: Callable[[], float] = time.monotonic,
                     discovery_state: dict | None = None, tracking_state: dict | None = None,
                     allow_lookup: bool = True) -> dict:
    """Refresh uncached links, preserving confirmed links during API outages.

    Unmatched/ambiguous links are retried daily. Confirmed IDs are reused while
    the latest Steam metadata and its release window are refreshed every run.
    """
    clock = _now(now)
    state = normalize_mapping_state(mapping_state)
    games = normalize_steam_catalog(catalog, clock) if isinstance(catalog, dict) else deepcopy(catalog)
    if not isinstance(games, list):
        raise ValueError("Steam catalog must be a catalog object or normalized list")
    reused = _reuse_discovery_mappings(state, games, discovery_state, tracking_state, clock)
    seen, due = set(), []
    for steam in games:
        if not isinstance(steam, dict):
            raise ValueError("Invalid normalized Steam game")
        appid = _id(steam.get("steam_appid"))
        if appid in seen:
            raise ValueError("Duplicate normalized Steam AppID")
        seen.add(appid)
        released, expires = parse_timestamp(steam["release_at"]), parse_timestamp(steam["expires_at"])
        if (expires != released + timedelta(days=30) or steam.get("release_date_timezone") != "Asia/Taipei"
                or released.astimezone(TAIPEI).date().isoformat() != steam.get("release_date")):
            raise ValueError("Inconsistent normalized Steam release window")
        steam["is_recent"] = released <= clock < expires
        entry = state["games"].setdefault(appid, {"steam_appid": appid, "status": "pending", "method": METHOD})
        entry["steam"] = steam
        entry["metadata_updated_at"] = timestamp(clock)
        if entry["status"] == "matched":
            continue
        if entry.get("retry_at") and parse_timestamp(entry["retry_at"]) > clock:
            continue
        due.append(appid)
    # Retained cache metadata may be old, but its window must never stay true forever.
    for entry in state["games"].values():
        steam = entry["steam"]
        recent = bool(entry["steam_appid"] in seen and
            parse_timestamp(steam["release_at"]) <= clock < parse_timestamp(steam["expires_at"]))
        if steam.get("is_recent") != recent:
            entry["metadata_updated_at"] = timestamp(clock)
        steam["is_recent"] = recent
    errors = []
    failures = (requests.RequestException, ValueError, KeyError, TypeError, RuntimeError, OverflowError, OSError)
    source = state.get("steam_source_id")
    if allow_lookup and due and not source:
        try:
            sources = _pages(client, "external_game_sources", "fields id,name;", deadline, monotonic)
            ids = {_id(row.get("id")) for row in sources if isinstance(row.get("name"), str) and row["name"].strip().casefold() == "steam"}
            if len(ids) != 1:
                raise ValueError("Steam external source is missing or ambiguous")
            source = state["steam_source_id"] = ids.pop()
        except failures as exc:
            errors.append({"stage": "external_game_sources", "reason": _error(exc)})
    if allow_lookup and source:
        for offset in range(0, len(due), 100):
            batch = due[offset:offset + 100]
            try:
                quoted = ",".join(f'"{appid}"' for appid in batch)
                rows = _pages(client, "external_games",
                    f"fields id,uid,game,external_game_source; where external_game_source = {source} & uid = ({quoted});", deadline, monotonic)
                links = {appid: set() for appid in batch}
                for row in rows:
                    # Enforce the source again locally; identical UIDs exist on other stores.
                    if str(row.get("external_game_source")) != source or str(row.get("uid")) not in links:
                        continue
                    links[str(row["uid"])].add(_id(row.get("game")))
                ids = sorted({next(iter(value)) for value in links.values() if len(value) == 1}, key=int)
                categories = {igdb_id: {} for igdb_id in ids}
                for start in range(0, len(ids), 100):
                    _deadline(deadline, monotonic)
                    payload = client.get("games", params=[("igdb_id", value) for value in ids[start:start + 100]])
                    _deadline(deadline, monotonic)
                    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                        raise ValueError("Invalid Helix games mapping response")
                    for row in payload["data"]:
                        if not isinstance(row, dict):
                            raise ValueError("Invalid Helix games mapping row")
                        igdb_id = str(row.get("igdb_id"))
                        if igdb_id not in categories:
                            continue
                        twitch_id = _id(row.get("id"))
                        if not isinstance(row.get("name"), str) or not row["name"].strip():
                            raise ValueError("Missing Twitch category name")
                        categories[igdb_id][twitch_id] = row
                # Commit the whole batch only after every required API page succeeds.
                for appid in batch:
                    entry = state["games"][appid]
                    for key in ("igdb_id", "twitch_game_id", "twitch_name", "box_art_url", "candidates"):
                        entry.pop(key, None)
                    entry.update(checked_at=timestamp(clock), retry_at=timestamp(clock + RETRY_INTERVAL), method=METHOD)
                    linked = links[appid]
                    if not linked:
                        entry.update(status="unmatched", reason="no_igdb_steam_link")
                    elif len(linked) > 1:
                        entry.update(status="ambiguous", reason="multiple_igdb_games", candidates=sorted(linked, key=int))
                    else:
                        igdb_id = next(iter(linked))
                        entry["igdb_id"] = igdb_id
                        matches = categories[igdb_id]
                        if not matches:
                            entry.update(status="unmatched", reason="no_twitch_category")
                        elif len(matches) > 1:
                            entry.update(status="ambiguous", reason="multiple_twitch_categories", candidates=sorted(matches, key=int))
                        else:
                            row = next(iter(matches.values()))
                            entry.update(status="matched", twitch_game_id=_id(row["id"]), twitch_name=row["name"],
                                box_art_url=row.get("box_art_url") if isinstance(row.get("box_art_url"), str) else None,
                                retry_at=None, reason="authoritative_id_chain")
            except failures as exc:
                errors.append({"stage": "mapping_batch", "reason": _error(exc)})
                if isinstance(exc, CollectionDeadlineExceeded):
                    break
    state["updated_at"] = timestamp(clock)
    if isinstance(catalog, dict):
        state["source_catalog"] = {"generated_at": catalog["generated_at"], "count": catalog["count"]}
        commit = catalog.get("source_commit") or catalog.get("_source_commit")
        if isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit):
            state["source_catalog"]["commit"] = commit
    counts = Counter(state["games"][appid]["status"] for appid in seen)
    state["report"] = {"status": "unavailable_or_partial" if errors else "ok", "catalog_count": len(games),
        "recent_count": sum(row["is_recent"] for row in games), "lookup_count": len(due) if allow_lookup else 0,
        "pending_lookup_count": len(due), "cached_discovery_mappings": len(reused),
        "metadata_only": not allow_lookup, "counts": dict(counts), "errors": errors}
    return state
