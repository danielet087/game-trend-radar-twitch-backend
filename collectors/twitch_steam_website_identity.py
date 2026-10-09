"""Bind one Twitch/IGDB identity to an official Steam website and store record.

This independent proof labels an existing Twitch category. It is not a public
catalog admission, a Steam recent-release membership, or a Followers lookup.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
import re
import time
from typing import Callable
from urllib.parse import urlsplit

import requests
from urllib3.util import Timeout

from collectors.steam_twitch_mapping import _deadline, _id, _now, _pages
from radar_backend.domain.twitch_newness import parse_timestamp, timestamp

METHOD = "twitch_igdb_steam_website_v1"
PROVIDER = "Steam Store appdetails cc=TW l=tchinese"
APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"
STORE_HOST = "store.steampowered.com"
APP_PATH = re.compile(r"/app/([1-9][0-9]*)(?:/[A-Za-z0-9_-]+)?/?\Z", re.ASCII)
ISO_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z", re.ASCII)
MONTHS = {name: index for index, names in enumerate((
    ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
    ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
    ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"), ("dec", "december"),
), 1) for name in names}


def _canonical_id(value) -> str:
    identity = _id(value)
    if isinstance(value, str) and value != identity:
        raise ValueError("Identity must be a canonical positive decimal ID")
    return identity


def _proof_id(value) -> str:
    if not isinstance(value, str):
        raise ValueError("Persisted proof identity must be a canonical string")
    return _canonical_id(value)


def _store_url(value: str) -> tuple[str, str] | None:
    """Never fetch supplied URLs; permit Steam AppID extraction only from HTTPS."""
    if (not isinstance(value, str) or not value or value != value.strip()
            or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value)
            or "\\" in value):
        raise ValueError("Invalid website URL")
    try:
        parts = urlsplit(value)
        host = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise ValueError("Invalid website URL") from exc
    if (parts.scheme not in {"http", "https"} or not host or parts.username is not None
            or parts.password is not None or port is not None or ":" in parts.netloc):
        raise ValueError("Unsafe website URL")
    if host != STORE_HOST:
        if "steampowered" in host.casefold():
            raise ValueError("Untrusted Steam-like website host")
        # Other IGDB website records, including video links with query strings,
        # provide no Steam identity and are never requested by this helper.
        return None
    match = APP_PATH.fullmatch(parts.path)
    if parts.scheme != "https" or "?" in value or "#" in value or not match:
        raise ValueError("Unsafe Steam application website")
    appid = _canonical_id(match.group(1))
    return appid, f"https://{STORE_HOST}/app/{appid}/"


def _store_day(raw: str) -> str | None:
    """Parse exact official TW/English dates; preserve uncertain text as unknown."""
    text = raw.strip()
    year = month = day = None
    try:
        if ISO_DAY.fullmatch(text):
            return date.fromisoformat(text).isoformat()
        chinese = re.fullmatch(r"([0-9]{4})\s*年\s*([0-9]{1,2})\s*月\s*([0-9]{1,2})\s*日", text)
        if chinese:
            year, month, day = map(int, chinese.groups())
        else:
            english = re.fullmatch(r"([0-9]{1,2})\s+([A-Za-z]+)\.?\s*,?\s*([0-9]{4})", text)
            reverse = re.fullmatch(r"([A-Za-z]+)\.?\s+([0-9]{1,2})\s*,?\s*([0-9]{4})", text)
            if english:
                day, label, year = english.groups()
            elif reverse:
                label, day, year = reverse.groups()
            else:
                return None
            month = MONTHS.get(label.casefold())
            if month is None:
                return None
            day, year = int(day), int(year)
        return date(year, month, day).isoformat()
    except (ValueError, TypeError, OverflowError):
        return None


def _descriptor_ids(value) -> list[int]:
    if not isinstance(value, list) or any(type(identity) is not int or identity < 0 for identity in value):
        raise ValueError("Steam content descriptors must be official integer IDs")
    if set(value) & {3, 4}:
        raise ValueError("Steam identity is excluded by official sexual content descriptors")
    return deepcopy(value)


def normalize_website_identity(value: dict, twitch_id, igdb_id) -> dict:
    """Validate the full cached proof and every ID, URL, date and screening binding."""
    expected_twitch, expected_igdb = _canonical_id(twitch_id), _canonical_id(igdb_id)
    required = {"method", "twitch_game_id", "igdb_id", "steam_appid", "website_links",
                "checked_at", "steam_identity_metadata"}
    if not isinstance(value, dict) or set(value) != required or value.get("method") != METHOD:
        raise ValueError("Invalid related Steam website identity proof")
    proof = deepcopy(value)
    if (_proof_id(proof["twitch_game_id"]) != expected_twitch
            or _proof_id(proof["igdb_id"]) != expected_igdb):
        raise ValueError("Steam website proof does not match its Twitch/IGDB identity")
    appid = _proof_id(proof["steam_appid"])
    canonical_url = f"https://{STORE_HOST}/app/{appid}/"
    parse_timestamp(proof["checked_at"])
    links = proof["website_links"]
    if not isinstance(links, list) or not links:
        raise ValueError("Steam website identity requires official website evidence")
    seen = set()
    for link in links:
        if not isinstance(link, dict) or set(link) != {"website_id", "game", "steam_appid", "source_url", "url"}:
            raise ValueError("Invalid Steam website evidence")
        website_id = _proof_id(link["website_id"])
        if (website_id in seen or _proof_id(link["game"]) != expected_igdb
                or _proof_id(link["steam_appid"]) != appid or link["url"] != canonical_url
                or _store_url(link["source_url"]) != (appid, canonical_url)):
            raise ValueError("Steam website evidence identity does not match")
        seen.add(website_id)

    metadata = proof["steam_identity_metadata"]
    fields = {"steam_appid", "steam_type", "display_name", "store_url", "sexual_content_screened",
              "content_descriptor_ids", "release_store_date", "release_date_raw", "raw_release_date",
              "checked_at", "provider"}
    if not isinstance(metadata, dict) or set(metadata) != fields:
        raise ValueError("Invalid official Steam identity metadata")
    if (_proof_id(metadata["steam_appid"]) != appid or metadata["steam_type"] != "game"
            or metadata["store_url"] != canonical_url or metadata["provider"] != PROVIDER
            or metadata["sexual_content_screened"] is not True
            or not isinstance(metadata["display_name"], str) or not metadata["display_name"].strip()
            or metadata["checked_at"] != proof["checked_at"]):
        raise ValueError("Official Steam metadata does not match the website proof")
    parse_timestamp(metadata["checked_at"])
    _descriptor_ids(metadata["content_descriptor_ids"])
    raw = metadata["raw_release_date"]
    if (not isinstance(raw, dict) or set(raw) != {"coming_soon", "date"}
            or type(raw["coming_soon"]) is not bool or not isinstance(raw["date"], str)
            or metadata["release_date_raw"] != raw["date"]
            or metadata["release_store_date"] != _store_day(raw["date"])):
        raise ValueError("Official Steam date metadata is inconsistent")
    return proof


def _steam_metadata(appid: str, checked_at: str, *, deadline: float | None,
                    monotonic: Callable[[], float]) -> dict:
    _deadline(deadline, monotonic)
    remaining = deadline - monotonic() if deadline is not None else 20.0
    timeout = Timeout(total=min(20.0, remaining), connect=min(10.0, remaining), read=min(20.0, remaining))
    # A separate unauthenticated request never forwards Twitch credentials and
    # never follows an IGDB-provided URL, a redirect, or a Steam Community path.
    response = requests.get(APPDETAILS_URL, params={"appids": appid, "cc": "tw", "l": "tchinese"},
                            timeout=timeout, allow_redirects=False)
    _deadline(deadline, monotonic)
    response.raise_for_status()
    if response.status_code != 200:
        raise ValueError("Steam identity metadata requires a complete HTTP 200 response")
    payload = response.json()
    _deadline(deadline, monotonic)
    if not isinstance(payload, dict) or set(payload) != {appid}:
        raise ValueError("Steam identity response does not match the requested AppID")
    row = payload[appid]
    if not isinstance(row, dict) or row.get("success") is not True or not isinstance(row.get("data"), dict):
        raise ValueError("Steam identity metadata is unavailable")
    steam = row["data"]
    if (_canonical_id(steam.get("steam_appid")) != appid or steam.get("type") != "game"
            or not isinstance(steam.get("name"), str) or not steam["name"].strip()):
        raise ValueError("Steam identity is not the requested official game")
    descriptors = steam.get("content_descriptors")
    if not isinstance(descriptors, dict):
        raise ValueError("Steam identity requires official content descriptors")
    ids = _descriptor_ids(descriptors.get("ids"))
    released = steam.get("release_date")
    if (not isinstance(released, dict) or type(released.get("coming_soon")) is not bool
            or not isinstance(released.get("date"), str)):
        raise ValueError("Steam identity requires a structured official release date")
    raw_date = released["date"]
    return {"steam_appid": appid, "steam_type": "game", "display_name": steam["name"].strip(),
            "store_url": f"https://{STORE_HOST}/app/{appid}/", "sexual_content_screened": True,
            "content_descriptor_ids": ids, "release_store_date": _store_day(raw_date),
            "release_date_raw": raw_date,
            "raw_release_date": {"coming_soon": released["coming_soon"], "date": raw_date},
            "checked_at": checked_at, "provider": PROVIDER}


def lookup_website_identity(client, twitch_id, igdb_id, now: datetime, *, deadline: float | None = None,
                            monotonic: Callable[[], float] = time.monotonic) -> dict | None:
    """Lookup exact IGDB website IDs, then verify their one Steam game keylessly."""
    twitch_id, igdb_id = _canonical_id(twitch_id), _canonical_id(igdb_id)
    checked_at = timestamp(_now(now))
    _deadline(deadline, monotonic)
    rows = _pages(client, "games", f"fields id,websites; where id = {igdb_id};", deadline, monotonic)
    if (not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict)
            or _canonical_id(rows[0].get("id")) != igdb_id):
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
    rows = _pages(client, "websites", f"fields id,url,game; where id = ({','.join(ordered_ids)});", deadline, monotonic)
    if not isinstance(rows, list):
        raise ValueError("IGDB websites must be a complete record list")
    requested, seen, links = set(website_ids), set(), []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Invalid IGDB website record")
        website_id, game = _canonical_id(row.get("id")), _canonical_id(row.get("game"))
        if website_id not in requested or website_id in seen or game != igdb_id:
            raise ValueError("IGDB website identity does not match its exact game")
        seen.add(website_id)
        steam = _store_url(row.get("url"))
        if steam is not None:
            appid, canonical_url = steam
            links.append({"website_id": website_id, "game": game, "steam_appid": appid,
                          "source_url": row["url"], "url": canonical_url})
    if seen != requested:
        raise ValueError("Partial IGDB website metadata cannot establish an identity")
    if not links:
        return None
    appids = {link["steam_appid"] for link in links}
    if len(appids) != 1:
        raise ValueError("IGDB websites contain multiple Steam application identities")
    appid = next(iter(appids))
    links.sort(key=lambda link: int(link["website_id"]))
    metadata = _steam_metadata(appid, checked_at, deadline=deadline, monotonic=monotonic)
    proof = {"method": METHOD, "twitch_game_id": twitch_id, "igdb_id": igdb_id,
             "steam_appid": appid, "website_links": links, "checked_at": checked_at,
             "steam_identity_metadata": metadata}
    _deadline(deadline, monotonic)
    return normalize_website_identity(proof, twitch_id, igdb_id)
