"""Refresh authoritative ID chains through explicit time and source ports."""

from __future__ import annotations
from datetime import datetime
from typing import Callable


def refresh_mappings(
    client,
    catalog: dict | list[dict],
    mapping_state: dict | None = None,
    now: datetime | None = None,
    *,
    deadline: float | None = None,
    monotonic: Callable[[], float],
    discovery_state: dict | None = None,
    tracking_state: dict | None = None,
    allow_lookup: bool = True,
    _now,
    normalize_mapping_state,
    normalize_steam_catalog,
    deepcopy,
    _reuse_discovery_mappings,
    _id,
    parse_timestamp,
    timedelta,
    TAIPEI,
    timestamp,
    METHOD,
    failure_types,
    _pages,
    _error,
    _deadline,
    RETRY_INTERVAL,
    CollectionDeadlineExceeded,
    re,
    Counter,
) -> dict:
    """Refresh uncached links, preserving confirmed links during API outages.

    Unmatched/ambiguous links are retried daily. Confirmed IDs are reused while
    the latest Steam metadata and its release window are refreshed every run.
    """
    clock = _now(now)
    state = normalize_mapping_state(mapping_state)
    games = (
        normalize_steam_catalog(catalog, clock)
        if isinstance(catalog, dict)
        else deepcopy(catalog)
    )
    if not isinstance(games, list):
        raise ValueError("Steam catalog must be a catalog object or normalized list")
    reused = _reuse_discovery_mappings(
        state, games, discovery_state, tracking_state, clock
    )
    seen, due = (set(), [])
    for steam in games:
        if not isinstance(steam, dict):
            raise ValueError("Invalid normalized Steam game")
        appid = _id(steam.get("steam_appid"))
        if appid in seen:
            raise ValueError("Duplicate normalized Steam AppID")
        seen.add(appid)
        released, expires = (
            parse_timestamp(steam["release_at"]),
            parse_timestamp(steam["expires_at"]),
        )
        if (
            expires != released + timedelta(days=30)
            or steam.get("release_date_timezone") != "Asia/Taipei"
            or released.astimezone(TAIPEI).date().isoformat()
            != steam.get("release_date")
        ):
            raise ValueError("Inconsistent normalized Steam release window")
        steam["is_recent"] = released <= clock < expires
        entry = state["games"].setdefault(
            appid, {"steam_appid": appid, "status": "pending", "method": METHOD}
        )
        entry["steam"] = steam
        entry["metadata_updated_at"] = timestamp(clock)
        if entry["status"] == "matched":
            continue
        if entry.get("retry_at") and parse_timestamp(entry["retry_at"]) > clock:
            continue
        due.append(appid)
    # Retained cache metadata may be old, but its window cannot stay true forever.
    for entry in state["games"].values():
        steam = entry["steam"]
        recent = bool(
            entry["steam_appid"] in seen
            and parse_timestamp(steam["release_at"])
            <= clock
            < parse_timestamp(steam["expires_at"])
        )
        if steam.get("is_recent") != recent:
            entry["metadata_updated_at"] = timestamp(clock)
        steam["is_recent"] = recent
    errors = []
    failures = failure_types()
    source = state.get("steam_source_id")
    if allow_lookup and due and (not source):
        try:
            sources = _pages(
                client, "external_game_sources", "fields id,name;", deadline, monotonic
            )
            ids = {
                _id(row.get("id"))
                for row in sources
                if isinstance(row.get("name"), str)
                and row["name"].strip().casefold() == "steam"
            }
            if len(ids) != 1:
                raise ValueError("Steam external source is missing or ambiguous")
            source = state["steam_source_id"] = ids.pop()
        except failures as exc:
            errors.append({"stage": "external_game_sources", "reason": _error(exc)})
    if allow_lookup and source:
        for offset in range(0, len(due), 100):
            batch = due[offset : offset + 100]
            try:
                quoted = ",".join((f'"{appid}"' for appid in batch))
                rows = _pages(
                    client,
                    "external_games",
                    f"fields id,uid,game,external_game_source; where external_game_source = {source} & uid = ({quoted});",
                    deadline,
                    monotonic,
                )
                links = {appid: set() for appid in batch}
                for row in rows:
                    # Enforce the source locally; identical UIDs exist on other stores.
                    if (
                        str(row.get("external_game_source")) != source
                        or str(row.get("uid")) not in links
                    ):
                        continue
                    links[str(row["uid"])].add(_id(row.get("game")))
                ids = sorted(
                    {next(iter(value)) for value in links.values() if len(value) == 1},
                    key=int,
                )
                categories = {igdb_id: {} for igdb_id in ids}
                for start in range(0, len(ids), 100):
                    _deadline(deadline, monotonic)
                    payload = client.get(
                        "games",
                        params=[
                            ("igdb_id", value) for value in ids[start : start + 100]
                        ],
                    )
                    _deadline(deadline, monotonic)
                    if not isinstance(payload, dict) or not isinstance(
                        payload.get("data"), list
                    ):
                        raise ValueError("Invalid Helix games mapping response")
                    for row in payload["data"]:
                        if not isinstance(row, dict):
                            raise ValueError("Invalid Helix games mapping row")
                        igdb_id = str(row.get("igdb_id"))
                        if igdb_id not in categories:
                            continue
                        twitch_id = _id(row.get("id"))
                        if (
                            not isinstance(row.get("name"), str)
                            or not row["name"].strip()
                        ):
                            raise ValueError("Missing Twitch category name")
                        categories[igdb_id][twitch_id] = row
                # Commit the batch only after every required API page succeeds.
                for appid in batch:
                    entry = state["games"][appid]
                    for key in (
                        "igdb_id",
                        "twitch_game_id",
                        "twitch_name",
                        "box_art_url",
                        "candidates",
                    ):
                        entry.pop(key, None)
                    entry.update(
                        checked_at=timestamp(clock),
                        retry_at=timestamp(clock + RETRY_INTERVAL),
                        method=METHOD,
                    )
                    linked = links[appid]
                    if not linked:
                        entry.update(status="unmatched", reason="no_igdb_steam_link")
                    elif len(linked) > 1:
                        entry.update(
                            status="ambiguous",
                            reason="multiple_igdb_games",
                            candidates=sorted(linked, key=int),
                        )
                    else:
                        igdb_id = next(iter(linked))
                        entry["igdb_id"] = igdb_id
                        matches = categories[igdb_id]
                        if not matches:
                            entry.update(
                                status="unmatched", reason="no_twitch_category"
                            )
                        elif len(matches) > 1:
                            entry.update(
                                status="ambiguous",
                                reason="multiple_twitch_categories",
                                candidates=sorted(matches, key=int),
                            )
                        else:
                            row = next(iter(matches.values()))
                            entry.update(
                                status="matched",
                                twitch_game_id=_id(row["id"]),
                                twitch_name=row["name"],
                                box_art_url=(
                                    row.get("box_art_url")
                                    if isinstance(row.get("box_art_url"), str)
                                    else None
                                ),
                                retry_at=None,
                                reason="authoritative_id_chain",
                            )
            except failures as exc:
                errors.append({"stage": "mapping_batch", "reason": _error(exc)})
                if isinstance(exc, CollectionDeadlineExceeded):
                    break
    state["updated_at"] = timestamp(clock)
    if isinstance(catalog, dict):
        state["source_catalog"] = {
            "generated_at": catalog["generated_at"],
            "count": catalog["count"],
        }
        commit = catalog.get("source_commit") or catalog.get("_source_commit")
        if isinstance(commit, str) and re.fullmatch("[0-9a-f]{40}", commit):
            state["source_catalog"]["commit"] = commit
    counts = Counter((state["games"][appid]["status"] for appid in seen))
    state["report"] = {
        "status": "unavailable_or_partial" if errors else "ok",
        "catalog_count": len(games),
        "recent_count": sum((row["is_recent"] for row in games)),
        "lookup_count": len(due) if allow_lookup else 0,
        "pending_lookup_count": len(due),
        "cached_discovery_mappings": len(reused),
        "metadata_only": not allow_lookup,
        "counts": dict(counts),
        "errors": errors,
    }
    return state
