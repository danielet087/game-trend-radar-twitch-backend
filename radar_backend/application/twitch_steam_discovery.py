"""Bounded reverse discovery using explicit clocks, identity sources and rules."""

from __future__ import annotations

from datetime import datetime
from typing import Callable


def refresh_discoveries(
    client,
    tracking_state: dict,
    public_catalog: dict,
    discovery_state: dict | None = None,
    now: datetime | None = None,
    *,
    deadline: float | None = None,
    monotonic: Callable[[], float],
    _now,
    timestamp,
    _catalog_ids,
    normalize_tracking_state,
    NON_GAME_IDS,
    _qualified_members,
    normalize_discovery_state,
    METHOD,
    parse_timestamp,
    POLICY_VERSION,
    _error,
    _deadline,
    _id,
    FAILURES,
    CollectionDeadlineExceeded,
    _pages,
    _invalidate_identity,
    RETRY_INTERVAL,
    TWITCH_IDENTITY_METHOD,
    deepcopy,
    Counter,
    website_lookup,
) -> dict:
    """Find Steam products for genuinely enrolled Twitch discoveries.

    Successful identity decisions are cached for 24 hours. API failures retain
    the previous decision and timestamp, allowing the next hourly run to retry.
    Public catalog membership is metadata, refreshed separately on every run.
    """
    clock = _now(now)
    at = timestamp(clock)
    public = _catalog_ids(public_catalog)
    tracking = normalize_tracking_state(
        tracking_state, clock, non_game_ids=NON_GAME_IDS
    )
    active = _qualified_members(tracking, clock)
    state = normalize_discovery_state(discovery_state)
    due = []
    for twitch_id, (entry, evidence) in active.items():
        source = entry["tracking_sources"]["twitch_new"]
        first_seen = source.get("first_seen_at") or entry["first_seen_at"]
        row = state["games"].setdefault(
            twitch_id,
            {
                "twitch_game_id": twitch_id,
                "twitch_name": entry["game_name"],
                "igdb_id": None,
                "status": "pending",
                "method": METHOD,
                "steam_appids": [],
                "links": [],
                "first_seen_at": first_seen,
                "checked_at": None,
                "retry_at": None,
                "twitch_enrollment": evidence,
                "active": True,
                "updated_at": at,
                "public_steam_appids": [],
                "missing_public_appids": [],
            },
        )
        row["first_seen_at"] = min(
            (row["first_seen_at"], first_seen), key=parse_timestamp
        )
        row.update(
            twitch_name=entry["game_name"],
            twitch_enrollment=evidence,
            active=True,
            updated_at=at,
        )
        upgraded_negative = (
            row["status"] in {"pending", "no_steam_link", "unavailable"}
            and row.get("lookup_policy_version", 1) < POLICY_VERSION
        )
        if (
            upgraded_negative
            or not row.get("retry_at")
            or parse_timestamp(row["retry_at"]) <= clock
        ):
            due.append(twitch_id)
    for twitch_id, row in state["games"].items():
        row["active"] = twitch_id in active
        row["updated_at"] = at

    errors = []

    def failure(stage: str, twitch_ids: list[str], exc: Exception) -> None:
        errors.append(
            {"stage": stage, "twitch_game_ids": list(twitch_ids), "reason": _error(exc)}
        )
        for twitch_id in twitch_ids:
            row = state["games"][twitch_id]
            if row["status"] in {"pending", "unavailable"}:
                row.update(
                    status="unavailable",
                    reason="official_lookup_unavailable",
                    retry_at=None,
                )

    source = state.get("steam_source_id")
    stopped = False
    due.sort(key=int)
    for offset in range(0, len(due), 100):
        batch = due[offset : offset + 100]
        try:
            _deadline(deadline, monotonic)
            response = client.get(
                "games", params=[("id", twitch_id) for twitch_id in batch]
            )
            _deadline(deadline, monotonic)
            if not isinstance(response, dict) or not isinstance(
                response.get("data"), list
            ):
                raise ValueError("Invalid Helix discovery response")
            categories = {}
            for category in response["data"]:
                if not isinstance(category, dict):
                    raise ValueError("Invalid Helix discovery category")
                twitch_id = _id(category.get("id"))
                if twitch_id not in batch:
                    continue
                if (
                    not isinstance(category.get("name"), str)
                    or not category["name"].strip()
                ):
                    raise ValueError("Helix discovery category requires a name")
                igdb_id = _id(category["igdb_id"]) if category.get("igdb_id") else None
                if (
                    twitch_id in categories
                    and categories[twitch_id]["igdb_id"] != igdb_id
                ):
                    raise ValueError("Conflicting Helix IGDB identities")
                categories[twitch_id] = {"name": category["name"], "igdb_id": igdb_id}
        except FAILURES as exc:
            failure("helix_games", batch, exc)
            if isinstance(exc, CollectionDeadlineExceeded):
                stopped = True
                break
            continue

        # Helix occasionally omits igdb_id even though IGDB already owns an
        # exact Twitch category UID. Only a returned Helix category can use this
        # fallback; a missing category cannot be restored from stale tracking.
        blanks = [
            twitch_id
            for twitch_id in batch
            if twitch_id in categories and categories[twitch_id]["igdb_id"] is None
        ]
        blocked = set()
        if blanks:
            try:
                twitch_source = state.get("twitch_source_id")
                if not twitch_source:
                    sources = _pages(
                        client,
                        "external_game_sources",
                        "fields id,name;",
                        deadline,
                        monotonic,
                    )
                    ids = {
                        _id(item.get("id"))
                        for item in sources
                        if isinstance(item.get("name"), str)
                        and item["name"].strip().casefold() == "twitch"
                    }
                    if len(ids) != 1:
                        raise ValueError(
                            "Twitch external source is missing or ambiguous"
                        )
                    twitch_source = state["twitch_source_id"] = ids.pop()
                quoted = ",".join(f'"{twitch_id}"' for twitch_id in blanks)
                external = _pages(
                    client,
                    "external_games",
                    f"fields id,uid,game,external_game_source; where external_game_source = {twitch_source} & uid = ({quoted});",
                    deadline,
                    monotonic,
                )
                fallback = {twitch_id: {} for twitch_id in blanks}
                for item in external:
                    uid = _id(item.get("uid"))
                    if (
                        item.get("uid") != uid
                        or _id(item.get("external_game_source")) != twitch_source
                        or uid not in fallback
                    ):
                        raise ValueError("Unexpected Twitch external identity")
                    external_id, igdb_id = _id(item.get("id")), _id(item.get("game"))
                    link = {
                        "external_game_id": external_id,
                        "external_game_source": twitch_source,
                        "uid": uid,
                        "game": igdb_id,
                    }
                    previous = fallback[uid].get(external_id)
                    if previous is not None and previous != link:
                        raise ValueError("Conflicting Twitch external identities")
                    fallback[uid][external_id] = link
                for twitch_id in blanks:
                    links = sorted(
                        fallback[twitch_id].values(),
                        key=lambda link: int(link["external_game_id"]),
                    )
                    identities = {link["game"] for link in links}
                    entry = active[twitch_id][0]
                    cached = entry.get("igdb_id") or (
                        entry.get("last_observation") or {}
                    ).get("igdb_id")
                    if cached is not None:
                        cached = _id(cached)
                    existing = state["games"][twitch_id].get("igdb_id")
                    reason = (
                        "ambiguous_twitch_igdb_identity"
                        if len(identities) > 1
                        else (
                            "conflicting_twitch_igdb_identity"
                            if identities
                            and (
                                (cached and cached not in identities)
                                or (existing and existing not in identities)
                            )
                            else (
                                "missing_twitch_igdb_identity"
                                if not identities
                                else None
                            )
                        )
                    )
                    if reason:
                        blocked.add(twitch_id)
                        row = state["games"][twitch_id]
                        if reason in {
                            "ambiguous_twitch_igdb_identity",
                            "conflicting_twitch_igdb_identity",
                        }:
                            _invalidate_identity(
                                row, reason, at, timestamp(clock + RETRY_INTERVAL)
                            )
                        elif row["status"] != "matched":
                            _invalidate_identity(
                                row, reason, at, timestamp(clock + RETRY_INTERVAL)
                            )
                        continue
                    igdb_id = identities.pop()
                    categories[twitch_id].update(
                        igdb_id=igdb_id,
                        igdb_identity={
                            "method": TWITCH_IDENTITY_METHOD,
                            "twitch_game_id": twitch_id,
                            "igdb_id": igdb_id,
                            "twitch_source_id": twitch_source,
                            "checked_at": at,
                            "links": links,
                        },
                    )
            except FAILURES as exc:
                blocked.update(blanks)
                failure("twitch_external_identity", blanks, exc)
                if isinstance(exc, CollectionDeadlineExceeded):
                    stopped = True
                    break

        searchable = [
            twitch_id
            for twitch_id in batch
            if twitch_id not in blocked
            and (categories.get(twitch_id) or {}).get("igdb_id")
        ]
        linked_ids = sorted(
            {categories[twitch_id]["igdb_id"] for twitch_id in searchable}, key=int
        )
        for twitch_id in searchable:
            row, current_igdb = (
                state["games"][twitch_id],
                categories[twitch_id]["igdb_id"],
            )
            if row.get("igdb_id") and row["igdb_id"] != current_igdb:
                # Unlike an outage, a fresh official category has positively
                # contradicted the old IGDB ownership. Do not retain its link.
                _invalidate_identity(
                    row,
                    "conflicting_current_igdb_identity",
                    at,
                    timestamp(clock + RETRY_INTERVAL),
                )
        for twitch_id in set(batch) - set(searchable) - blocked:
            row = state["games"][twitch_id]
            if row["status"] != "matched":
                # A completed missing-identity decision must not leave an
                # independent website proof attached to a pending row.
                # There is no current owner against which to bind that proof.
                _invalidate_identity(
                    row,
                    "missing_twitch_igdb_identity",
                    at,
                    timestamp(clock + RETRY_INTERVAL),
                )
        if not searchable:
            continue
        if not source:
            try:
                sources = _pages(
                    client,
                    "external_game_sources",
                    "fields id,name;",
                    deadline,
                    monotonic,
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
            except FAILURES as exc:
                failure("external_game_sources", searchable, exc)
                if isinstance(exc, CollectionDeadlineExceeded):
                    stopped = True
                    break
                continue
        try:
            query = (
                "fields id,uid,game,external_game_source; where external_game_source = "
                + source
                + " & game = ("
                + ",".join(linked_ids)
                + ");"
            )
            external = _pages(client, "external_games", query, deadline, monotonic)
            links_by_igdb = {igdb_id: {} for igdb_id in linked_ids}
            for item in external:
                # Identical UIDs on other services must never become Steam IDs.
                if (
                    str(item.get("external_game_source")) != source
                    or str(item.get("game")) not in links_by_igdb
                ):
                    continue
                external_id, igdb_id, appid = (
                    _id(item.get("id")),
                    _id(item.get("game")),
                    _id(item.get("uid")),
                )
                link = {
                    "external_game_id": external_id,
                    "external_game_source": source,
                    "uid": appid,
                    "game": igdb_id,
                    "steam_appid": appid,
                    "url": f"https://store.steampowered.com/app/{appid}/",
                }
                previous = links_by_igdb[igdb_id].get(external_id)
                if previous is not None and previous != link:
                    raise ValueError("Conflicting IGDB external identities")
                links_by_igdb[igdb_id][external_id] = link
            # Commit a batch only after all pages have completed successfully.
            for twitch_id in searchable:
                category = categories[twitch_id]
                links = sorted(
                    links_by_igdb[category["igdb_id"]].values(),
                    key=lambda link: int(link["external_game_id"]),
                )
                appids = sorted({link["steam_appid"] for link in links}, key=int)
                row = state["games"][twitch_id]
                row.update(
                    twitch_name=category["name"],
                    igdb_id=category["igdb_id"],
                    links=links,
                    steam_appids=appids,
                    status="matched" if appids else "no_steam_link",
                    checked_at=at,
                    retry_at=timestamp(clock + RETRY_INTERVAL),
                    lookup_policy_version=POLICY_VERSION,
                    reason="authoritative_id_chain" if appids else "no_igdb_steam_link",
                )
                row.pop("igdb_identity", None)
                if category.get("igdb_identity"):
                    row["igdb_identity"] = deepcopy(category["igdb_identity"])
                if appids:
                    for key in (
                        "related_steam_identity",
                        "related_lookup_status",
                        "related_checked_at",
                        "related_retry_at",
                    ):
                        row.pop(key, None)
                else:
                    # The complete direct lookup has its own 24-hour cache.
                    # Website/Store failures can retry next hourly collection
                    # without forcing another direct external_games request.
                    row["related_retry_at"] = None
        except FAILURES as exc:
            failure("external_games", searchable, exc)
            if isinstance(exc, CollectionDeadlineExceeded):
                stopped = True
                break

    related_lookup_count = 0
    if not stopped:
        lookup_website_identity = website_lookup()
        for twitch_id in sorted(active, key=int):
            row = state["games"][twitch_id]
            if row["status"] != "no_steam_link" or not row.get("igdb_id"):
                continue
            if (
                row.get("related_retry_at")
                and parse_timestamp(row["related_retry_at"]) > clock
            ):
                continue
            related_lookup_count += 1
            try:
                identity = lookup_website_identity(
                    client,
                    twitch_id,
                    row["igdb_id"],
                    clock,
                    deadline=deadline,
                    monotonic=monotonic,
                )
                row.update(
                    related_lookup_status="matched" if identity else "no_steam_website",
                    related_checked_at=at,
                    related_retry_at=timestamp(clock + RETRY_INTERVAL),
                )
                if identity:
                    row["related_steam_identity"] = identity
                else:
                    row.pop("related_steam_identity", None)
            except FAILURES as exc:
                # A cached website identity remains useful only while the
                # canonical IGDB owner is unchanged. Its checked_at is real.
                row.update(related_lookup_status="unavailable", related_retry_at=None)
                errors.append(
                    {
                        "stage": "igdb_steam_website",
                        "twitch_game_ids": [twitch_id],
                        "reason": _error(exc),
                    }
                )
                if isinstance(exc, CollectionDeadlineExceeded):
                    stopped = True
                    break

    for row in state["games"].values():
        row["public_steam_appids"] = sorted(set(row["steam_appids"]) & public, key=int)
        row["missing_public_appids"] = sorted(
            set(row["steam_appids"]) - public, key=int
        )
    state["updated_at"] = at
    state["source_catalog"] = {
        "generated_at": public_catalog["generated_at"],
        "count": public_catalog["count"],
        "appids": sorted(public, key=int),
    }
    counts = Counter(state["games"][twitch_id]["status"] for twitch_id in active)
    state["report"] = {
        "status": "unavailable_or_partial" if errors else "ok",
        "active_twitch_games": len(active),
        "lookup_count": len(due),
        "counts": dict(counts),
        "errors": errors,
        "related_lookup_count": related_lookup_count,
        "deadline_exhausted": stopped,
        "missing_public_appids": sorted(
            {
                appid
                for twitch_id in active
                for appid in state["games"][twitch_id]["missing_public_appids"]
            },
            key=int,
        ),
    }
    return normalize_discovery_state(state)
