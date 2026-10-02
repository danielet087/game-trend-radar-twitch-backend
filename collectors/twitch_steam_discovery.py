"""Reverse official Twitch -> IGDB -> Steam discovery, without name matching.

This registry only proposes Steam AppIDs. The Steam consumer must verify the
same snapshot's Twitch enrollment and apply its official store/content gates.
Missing IGDB links never prove that a game has no Steam version.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta
import time
from typing import Callable

import requests

from collectors.steam_twitch_mapping import _deadline, _error, _id, _now, _pages
from collectors.twitch_live import CollectionDeadlineExceeded
from collectors.twitch_newness import parse_timestamp, timestamp
from collectors.twitch_tracking import normalize_tracking_state

METHOD = "twitch_igdb_external_steam_v1"
RETRY_INTERVAL = timedelta(hours=24)
STATUSES = {"matched", "no_steam_link", "pending", "unavailable"}
ENROLLMENT_SOURCES = {"igdb_first_release_date", "twitch_original_release_date", "twitch_directory_dom"}
NON_GAME_IDS = {"509658", "509672", "509663", "509659", "26936"}
FAILURES = (requests.RequestException, ValueError, KeyError, TypeError, RuntimeError, OverflowError, OSError)


def _ids(value) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("Discovery IDs must be a list")
    ids = [_id(item) for item in value]
    if len(set(ids)) != len(ids):
        raise ValueError("Discovery IDs must be unique")
    return sorted(ids, key=int)


def _enrollment(value, clock: datetime | None = None) -> dict:
    if (not isinstance(value, dict) or value.get("source") not in ENROLLMENT_SOURCES
            or type(value.get("min_viewers")) is not int or value["min_viewers"] != 7000
            or type(value.get("viewer_count")) is not int or value["viewer_count"] < value["min_viewers"]
            or value.get("qualification") == "unverified"):
        raise ValueError("Discovery requires a qualified Twitch enrollment")
    observed = parse_timestamp(value.get("observed_at"))
    if clock is not None and observed > clock:
        raise ValueError("Twitch enrollment cannot be from the future")
    return deepcopy(value)


def normalize_discovery_state(payload: dict | None = None) -> dict:
    """Copy and validate persisted identities and their exact official evidence."""
    if payload is None:
        return {"schema_version": 1, "updated_at": None, "games": {}}
    if (not isinstance(payload, dict) or type(payload.get("schema_version")) is not int or payload["schema_version"] != 1
            or not isinstance(payload.get("games"), dict)):
        raise ValueError("Invalid Twitch/Steam discovery registry")
    state = deepcopy(payload)
    if state.get("updated_at") is not None:
        parse_timestamp(state["updated_at"])
    source = _id(state["steam_source_id"]) if state.get("steam_source_id") is not None else None
    if source is not None:
        state["steam_source_id"] = source
    if state.get("source_catalog") is not None:
        catalog = state["source_catalog"]
        if not isinstance(catalog, dict) or type(catalog.get("count")) is not int:
            raise ValueError("Invalid discovery catalog source")
        parse_timestamp(catalog.get("generated_at"))
        catalog["appids"] = _ids(catalog.get("appids"))
        if catalog["count"] != len(catalog["appids"]):
            raise ValueError("Discovery catalog source count does not match")
    for twitch_id, row in state["games"].items():
        if (not isinstance(twitch_id, str) or _id(twitch_id) != twitch_id
                or not isinstance(row, dict) or _id(row.get("twitch_game_id")) != twitch_id
                or row.get("status") not in STATUSES or row.get("method") != METHOD
                or not isinstance(row.get("twitch_name"), str) or not row["twitch_name"].strip()
                or type(row.get("active")) is not bool):
            raise ValueError("Invalid discovery identity, status, or provenance")
        _enrollment(row.get("twitch_enrollment"))
        parse_timestamp(row.get("first_seen_at"))
        parse_timestamp(row.get("updated_at"))
        for key in ("checked_at", "retry_at"):
            if row.get(key) is not None:
                parse_timestamp(row[key])
        row["igdb_id"] = _id(row["igdb_id"]) if row.get("igdb_id") is not None else None
        appids = row["steam_appids"] = _ids(row.get("steam_appids"))
        if not isinstance(row.get("links"), list):
            raise ValueError("Discovery requires official external links")
        covered, identities = set(), set()
        for link in row["links"]:
            if not isinstance(link, dict):
                raise ValueError("Invalid discovery external link")
            appid = _id(link.get("steam_appid"))
            external_id = _id(link.get("external_game_id"))
            if (not source or _id(link.get("external_game_source")) != source
                    or _id(link.get("game")) != row["igdb_id"]
                    or link.get("uid") != appid or appid not in appids
                    or link.get("url") != f"https://store.steampowered.com/app/{appid}/"
                    or external_id in identities):
                raise ValueError("Discovery external identity does not match")
            identities.add(external_id)
            covered.add(appid)
        if covered != set(appids):
            raise ValueError("Discovery AppIDs require corresponding external evidence")
        if row["status"] == "matched":
            if not appids or not row["igdb_id"] or not row.get("checked_at"):
                raise ValueError("Matched discovery requires checked official identities")
        elif appids or row["links"]:
            raise ValueError("Unconfirmed discovery cannot carry confirmed links")
        if row["status"] == "no_steam_link" and (not source or not row["igdb_id"] or not row.get("checked_at")):
            raise ValueError("Missing Steam link requires a successful official lookup")
        public = row["public_steam_appids"] = _ids(row.get("public_steam_appids", []))
        missing = row["missing_public_appids"] = _ids(row.get("missing_public_appids", []))
        if set(public) & set(missing) or set(public) | set(missing) != set(appids):
            raise ValueError("Discovery catalog membership does not match its AppIDs")
        if state.get("source_catalog") is not None:
            catalog_ids = set(state["source_catalog"]["appids"])
            if set(public) != set(appids) & catalog_ids or set(missing) != set(appids) - catalog_ids:
                raise ValueError("Discovery catalog membership does not match its source snapshot")
    return state


def _catalog_ids(payload: dict) -> set[str]:
    if (not isinstance(payload, dict) or payload.get("version") != 2
            or not isinstance(payload.get("games"), list) or type(payload.get("count")) is not int
            or payload["count"] != len(payload["games"])):
        raise ValueError("Invalid public Steam catalog envelope")
    parse_timestamp(payload.get("generated_at"))
    ids = []
    for row in payload["games"]:
        if not isinstance(row, dict):
            raise ValueError("Invalid public Steam catalog game")
        ids.append(_id(row.get("appid")))
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate public Steam AppID")
    return set(ids)


def _qualified_members(tracking: dict, clock: datetime) -> dict[str, tuple[dict, dict]]:
    result = {}
    for twitch_id, entry in tracking["games"].items():
        source = entry.get("tracking_sources", {}).get("twitch_new")
        if (twitch_id in NON_GAME_IDS or entry.get("status") != "active" or not isinstance(source, dict)
                or source.get("source") != "twitch_new" or source.get("status") != "active"):
            continue
        try:
            evidence = _enrollment(source.get("enrollment"), clock)
        except (ValueError, TypeError):
            # Legacy manual restorations and Steam admissions cannot create
            # a second automatic source or evade the original Twitch gate.
            continue
        result[_id(twitch_id)] = (entry, evidence)
    return result


def refresh_discoveries(client, tracking_state: dict, public_catalog: dict,
                        discovery_state: dict | None = None, now: datetime | None = None, *,
                        deadline: float | None = None, monotonic: Callable[[], float] = time.monotonic) -> dict:
    """Find Steam products for genuinely enrolled Twitch discoveries.

    Successful identity decisions are cached for 24 hours. API failures retain
    the previous decision and timestamp, allowing the next hourly run to retry.
    Public catalog membership is metadata, refreshed separately on every run.
    """
    clock = _now(now)
    at = timestamp(clock)
    public = _catalog_ids(public_catalog)
    tracking = normalize_tracking_state(tracking_state, clock, non_game_ids=NON_GAME_IDS)
    active = _qualified_members(tracking, clock)
    state = normalize_discovery_state(discovery_state)
    due = []
    for twitch_id, (entry, evidence) in active.items():
        source = entry["tracking_sources"]["twitch_new"]
        first_seen = source.get("first_seen_at") or entry["first_seen_at"]
        row = state["games"].setdefault(twitch_id, {
            "twitch_game_id": twitch_id, "twitch_name": entry["game_name"], "igdb_id": None,
            "status": "pending", "method": METHOD, "steam_appids": [], "links": [],
            "first_seen_at": first_seen, "checked_at": None, "retry_at": None,
            "twitch_enrollment": evidence, "active": True, "updated_at": at,
            "public_steam_appids": [], "missing_public_appids": [],
        })
        row["first_seen_at"] = min((row["first_seen_at"], first_seen), key=parse_timestamp)
        row.update(twitch_name=entry["game_name"], twitch_enrollment=evidence, active=True, updated_at=at)
        if not row.get("retry_at") or parse_timestamp(row["retry_at"]) <= clock:
            due.append(twitch_id)
    for twitch_id, row in state["games"].items():
        row["active"] = twitch_id in active
        row["updated_at"] = at

    errors = []

    def failure(stage: str, twitch_ids: list[str], exc: Exception) -> None:
        errors.append({"stage": stage, "twitch_game_ids": list(twitch_ids), "reason": _error(exc)})
        for twitch_id in twitch_ids:
            row = state["games"][twitch_id]
            if row["status"] in {"pending", "unavailable"}:
                row.update(status="unavailable", reason="official_lookup_unavailable")

    source = state.get("steam_source_id")
    stopped = False
    due.sort(key=int)
    for offset in range(0, len(due), 100):
        batch = due[offset:offset + 100]
        try:
            _deadline(deadline, monotonic)
            response = client.get("games", params=[("id", twitch_id) for twitch_id in batch])
            _deadline(deadline, monotonic)
            if not isinstance(response, dict) or not isinstance(response.get("data"), list):
                raise ValueError("Invalid Helix discovery response")
            categories = {}
            for category in response["data"]:
                if not isinstance(category, dict):
                    raise ValueError("Invalid Helix discovery category")
                twitch_id = _id(category.get("id"))
                if twitch_id not in batch:
                    continue
                if not isinstance(category.get("name"), str) or not category["name"].strip():
                    raise ValueError("Helix discovery category requires a name")
                igdb_id = _id(category["igdb_id"]) if category.get("igdb_id") else None
                if twitch_id in categories and categories[twitch_id]["igdb_id"] != igdb_id:
                    raise ValueError("Conflicting Helix IGDB identities")
                categories[twitch_id] = {"name": category["name"], "igdb_id": igdb_id}
        except FAILURES as exc:
            failure("helix_games", batch, exc)
            if isinstance(exc, CollectionDeadlineExceeded):
                stopped = True
                break
            continue

        linked_ids = sorted({category["igdb_id"] for category in categories.values() if category["igdb_id"]}, key=int)
        searchable = [twitch_id for twitch_id in batch if (categories.get(twitch_id) or {}).get("igdb_id")]
        for twitch_id in set(batch) - set(searchable):
            row = state["games"][twitch_id]
            if row["status"] != "matched":
                row.update(status="pending", reason="missing_twitch_igdb_identity", retry_at=timestamp(clock + RETRY_INTERVAL))
        if not searchable:
            continue
        if not source:
            try:
                sources = _pages(client, "external_game_sources", "fields id,name;", deadline, monotonic)
                ids = {_id(row.get("id")) for row in sources if isinstance(row.get("name"), str)
                       and row["name"].strip().casefold() == "steam"}
                if len(ids) != 1:
                    raise ValueError("Steam external source is missing or ambiguous")
                source = state["steam_source_id"] = ids.pop()
            except FAILURES as exc:
                failure("external_game_sources", searchable, exc)
                if isinstance(exc, CollectionDeadlineExceeded):
                    stopped = True
                    break
                continue
        try:
            query = ("fields id,uid,game,external_game_source; where external_game_source = " + source
                     + " & game = (" + ",".join(linked_ids) + ");")
            external = _pages(client, "external_games", query, deadline, monotonic)
            links_by_igdb = {igdb_id: {} for igdb_id in linked_ids}
            for item in external:
                # Identical UIDs on other services must never become Steam IDs.
                if str(item.get("external_game_source")) != source or str(item.get("game")) not in links_by_igdb:
                    continue
                external_id, igdb_id, appid = _id(item.get("id")), _id(item.get("game")), _id(item.get("uid"))
                link = {"external_game_id": external_id, "external_game_source": source, "uid": appid,
                        "game": igdb_id, "steam_appid": appid, "url": f"https://store.steampowered.com/app/{appid}/"}
                previous = links_by_igdb[igdb_id].get(external_id)
                if previous is not None and previous != link:
                    raise ValueError("Conflicting IGDB external identities")
                links_by_igdb[igdb_id][external_id] = link
            # Commit a batch only after all pages have completed successfully.
            for twitch_id in searchable:
                category = categories[twitch_id]
                links = sorted(links_by_igdb[category["igdb_id"]].values(), key=lambda link: int(link["external_game_id"]))
                appids = sorted({link["steam_appid"] for link in links}, key=int)
                row = state["games"][twitch_id]
                row.update(twitch_name=category["name"], igdb_id=category["igdb_id"], links=links, steam_appids=appids,
                           status="matched" if appids else "no_steam_link", checked_at=at,
                           retry_at=timestamp(clock + RETRY_INTERVAL),
                           reason="authoritative_id_chain" if appids else "no_igdb_steam_link")
        except FAILURES as exc:
            failure("external_games", searchable, exc)
            if isinstance(exc, CollectionDeadlineExceeded):
                stopped = True
                break

    for row in state["games"].values():
        row["public_steam_appids"] = sorted(set(row["steam_appids"]) & public, key=int)
        row["missing_public_appids"] = sorted(set(row["steam_appids"]) - public, key=int)
    state["updated_at"] = at
    state["source_catalog"] = {"generated_at": public_catalog["generated_at"], "count": public_catalog["count"],
                               "appids": sorted(public, key=int)}
    counts = Counter(state["games"][twitch_id]["status"] for twitch_id in active)
    state["report"] = {"status": "unavailable_or_partial" if errors else "ok", "active_twitch_games": len(active),
                       "lookup_count": len(due), "counts": dict(counts), "errors": errors,
                       "deadline_exhausted": stopped,
                       "missing_public_appids": sorted({appid for twitch_id in active
                           for appid in state["games"][twitch_id]["missing_public_appids"]}, key=int)}
    return normalize_discovery_state(state)
