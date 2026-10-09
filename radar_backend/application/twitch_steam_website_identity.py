"""Coordinate the exact website chain through explicit clock and lookup ports."""

from __future__ import annotations

from datetime import datetime
from typing import Callable


def lookup_website_identity(
    client,
    twitch_id,
    igdb_id,
    now: datetime,
    *,
    deadline: float | None,
    monotonic: Callable[[], float],
    _canonical_id,
    timestamp,
    _now,
    _deadline,
    _pages,
    _store_url,
    _steam_metadata,
    normalize_website_identity,
    method,
) -> dict | None:
    """Lookup exact IGDB website IDs, then verify their one Steam game keylessly."""
    twitch_id, igdb_id = (_canonical_id(twitch_id), _canonical_id(igdb_id))
    checked_at = timestamp(_now(now))
    _deadline(deadline, monotonic)
    rows = _pages(
        client,
        "games",
        f"fields id,websites; where id = {igdb_id};",
        deadline,
        monotonic,
    )
    if (
        not isinstance(rows, list)
        or len(rows) != 1
        or (not isinstance(rows[0], dict))
        or (_canonical_id(rows[0].get("id")) != igdb_id)
    ):
        raise ValueError("IGDB website lookup requires one exact game identity")
    website_ids = rows[0].get("websites", [])
    if not isinstance(website_ids, list):
        raise ValueError("IGDB game websites must be an ID list")
    website_ids = [_canonical_id(identity) for identity in website_ids]
    if len(set(website_ids)) != len(website_ids):
        raise ValueError("IGDB game website IDs must be unique")
    if not website_ids:
        return None
    ordered_ids = sorted(website_ids, key=int)
    rows = _pages(
        client,
        "websites",
        f"fields id,url,game; where id = ({','.join(ordered_ids)});",
        deadline,
        monotonic,
    )
    if not isinstance(rows, list):
        raise ValueError("IGDB websites must be a complete record list")
    requested, seen, links = (set(website_ids), set(), [])
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Invalid IGDB website record")
        website_id, game = (
            _canonical_id(row.get("id")),
            _canonical_id(row.get("game")),
        )
        if website_id not in requested or website_id in seen or game != igdb_id:
            raise ValueError("IGDB website identity does not match its exact game")
        seen.add(website_id)
        steam = _store_url(row.get("url"))
        if steam is not None:
            appid, canonical_url = steam
            links.append(
                {
                    "website_id": website_id,
                    "game": game,
                    "steam_appid": appid,
                    "source_url": row["url"],
                    "url": canonical_url,
                }
            )
    if seen != requested:
        raise ValueError("Partial IGDB website metadata cannot establish an identity")
    if not links:
        return None
    appids = {link["steam_appid"] for link in links}
    if len(appids) != 1:
        raise ValueError("IGDB websites contain multiple Steam application identities")
    appid = next(iter(appids))
    links.sort(key=lambda link: int(link["website_id"]))
    metadata = _steam_metadata(
        appid, checked_at, deadline=deadline, monotonic=monotonic
    )
    proof = {
        "method": method(),
        "twitch_game_id": twitch_id,
        "igdb_id": igdb_id,
        "steam_appid": appid,
        "website_links": links,
        "checked_at": checked_at,
        "steam_identity_metadata": metadata,
    }
    _deadline(deadline, monotonic)
    return normalize_website_identity(proof, twitch_id, igdb_id)
