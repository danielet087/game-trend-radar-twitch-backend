"""Pure validation, release windows and authoritative mapping-cache rules."""

from __future__ import annotations
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import re
from zoneinfo import ZoneInfo
from radar_backend.domain.twitch_newness import parse_timestamp, timestamp
from radar_core.domain.twitch_admission import (
    TW_STORE_DATE_AUTHORITY,
    has_taiwan_store_date_authority,
    is_twitch_qualified,
)

TAIPEI = ZoneInfo("Asia/Taipei")
METHOD = "steam_appid_igdb_external_games_helix_igdb_id"
DAY = re.compile("\\d{4}-\\d{2}-\\d{2}\\Z")


def _clock(clock: datetime) -> datetime:
    if clock.tzinfo is None:
        raise ValueError("Clock must include a timezone")
    return clock.astimezone(timezone.utc)


def _id(value) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("ID must be a positive decimal integer")
    text = str(value)
    if not text.isascii() or not text.isdigit() or int(text) <= 0:
        raise ValueError("ID must be a positive decimal integer")
    return str(int(text))


def _strings(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(
        dict.fromkeys(
            (item.strip() for item in value if isinstance(item, str) and item.strip())
        )
    )


def _labels(value, allowed: list[str]) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        key: value[key].strip()
        for key in allowed
        if isinstance(value.get(key), str) and value[key].strip()
    }


def normalize_mapping_state(
    payload: dict | None,
    *,
    deepcopy=deepcopy,
    parse_timestamp=parse_timestamp,
    _id=_id,
    METHOD=METHOD,
) -> dict:
    """Validate persisted identity links without reinterpreting their statuses."""
    if payload is None:
        return {"schema_version": 1, "updated_at": None, "games": {}}
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or (not isinstance(payload.get("games"), dict))
    ):
        raise ValueError("Invalid Steam/Twitch mapping registry")
    state = deepcopy(payload)
    if state.get("updated_at") is not None:
        parse_timestamp(state["updated_at"])
    if state.get("steam_source_id") is not None:
        state["steam_source_id"] = _id(state["steam_source_id"])
    for appid, row in state["games"].items():
        if (
            not isinstance(appid, str)
            or _id(appid) != appid
            or (not isinstance(row, dict))
        ):
            raise ValueError("Invalid mapping AppID or record")
        if _id(row.get("steam_appid")) != appid or row.get("status") not in {
            "matched",
            "unmatched",
            "ambiguous",
            "pending",
        }:
            raise ValueError("Invalid mapping identity or status")
        if (
            not isinstance(row.get("steam"), dict)
            or _id(row["steam"].get("steam_appid")) != appid
        ):
            raise ValueError("Mapping requires corresponding Steam metadata")
        for key in ("checked_at", "retry_at", "metadata_updated_at"):
            if row.get(key) is not None:
                parse_timestamp(row[key])
        for key in ("igdb_id", "twitch_game_id"):
            if row.get(key) is not None:
                row[key] = _id(row[key])
        if row["status"] == "matched":
            if (
                not row.get("igdb_id")
                or not row.get("twitch_game_id")
                or row.get("method") != METHOD
            ):
                raise ValueError(
                    "Confirmed mapping requires authoritative IDs and provenance"
                )
    return state


def normalize_steam_catalog(
    payload: dict,
    now: datetime,
    *,
    parse_timestamp=parse_timestamp,
    _id=_id,
    has_taiwan_store_date_authority=has_taiwan_store_date_authority,
    is_twitch_qualified=is_twitch_qualified,
    DAY=DAY,
    date=date,
    datetime=datetime,
    TAIPEI=TAIPEI,
    timezone=timezone,
    _strings=_strings,
    _labels=_labels,
    timedelta=timedelta,
    timestamp=timestamp,
    TW_STORE_DATE_AUTHORITY=TW_STORE_DATE_AUTHORITY,
    deepcopy=deepcopy,
    _clock=_clock,
) -> list[dict]:
    """Read the curated public catalog; uncertain/conflicting release rows cannot enroll.

    An exact UTC release instant takes precedence over Taipei midnight. Future
    releases remain useful for identity links, but are never recent releases.
    """
    clock = _clock(now)
    if (
        not isinstance(payload, dict)
        or payload.get("version") != 2
        or (not isinstance(payload.get("games"), list))
        or (type(payload.get("count")) is not int)
        or (payload["count"] != len(payload["games"]))
    ):
        raise ValueError("Invalid curated Steam catalog envelope")
    parse_timestamp(payload.get("generated_at"))
    result, seen = ([], set())
    for row in payload["games"]:
        if not isinstance(row, dict):
            raise ValueError("Steam catalog game must be an object")
        appid = _id(row.get("appid"))
        if appid in seen:
            raise ValueError("Duplicate Steam catalog AppID")
        seen.add(appid)
        start, end = (row.get("release_start"), row.get("release_end"))
        taiwan_authority = has_taiwan_store_date_authority(row) and is_twitch_qualified(
            row
        )
        if (
            row.get("release_precision") != "day"
            or not isinstance(start, str)
            or (not DAY.fullmatch(start))
            or (start != end)
            or (row.get("release_date_conflict") is True and (not taiwan_authority))
            or (row.get("release_date_timezone", "Asia/Taipei") != "Asia/Taipei")
        ):
            continue
        try:
            release_day = date.fromisoformat(start)
            if row.get("release_time_utc"):
                release = parse_timestamp(row["release_time_utc"])
                if release.astimezone(TAIPEI).date() != release_day and (
                    not taiwan_authority
                ):
                    continue
            else:
                release = datetime.combine(
                    release_day, datetime.min.time(), TAIPEI
                ).astimezone(timezone.utc)
            if row.get("release_timestamp_taipei_date", start) != start and (
                not taiwan_authority
            ):
                continue
            if taiwan_authority:
                # The verified TW store day governs the release window. Keep
                # the other official timestamp below as diagnostic evidence.
                release = datetime.combine(
                    release_day, datetime.min.time(), TAIPEI
                ).astimezone(timezone.utc)
        except (ValueError, TypeError, OverflowError):
            continue
        name = row.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Steam catalog game requires a name")
        display = row.get("display_name") or name
        english = row.get("name_en") or name
        followers = row.get("followers")
        unknown_followers = followers is None and is_twitch_qualified(row)
        if (
            not isinstance(display, str)
            or not isinstance(english, str)
            or not (type(followers) is int and followers >= 0 or unknown_followers)
        ):
            raise ValueError("Invalid Steam catalog display metadata")
        tags, genres = (_strings(row.get("tags")), _strings(row.get("genres")))
        expires = release + timedelta(days=30)
        result.append(
            {
                "steam_appid": appid,
                "name": name.strip(),
                "display_name": display.strip(),
                "name_en": english.strip(),
                "store_url": f"https://store.steampowered.com/app/{appid}/",
                "followers": followers,
                **(
                    {
                        key: deepcopy(row[key])
                        for key in (
                            "follower_checked_at", "follower_source", "official_ge5000",
                            "follower_status", "follower_unavailable_at", "twitch_admission",
                        )
                    }
                    if unknown_followers
                    else {}
                ),
                "release_at": timestamp(release),
                "release_date": start,
                "release_date_timezone": "Asia/Taipei",
                "release_precision": "day",
                "release_time_basis": (
                    TW_STORE_DATE_AUTHORITY
                    if taiwan_authority
                    else (
                        "exact_utc"
                        if row.get("release_time_utc")
                        else "taipei_date_midnight"
                    )
                ),
                "is_recent": release <= clock < expires,
                "expires_at": timestamp(expires),
                "tags": tags,
                "genres": genres,
                "tag_labels_zh_tw": _labels(row.get("tag_labels_zh_tw"), tags),
                "genre_labels_zh_tw": _labels(row.get("genre_labels_zh_tw"), genres),
                **(
                    {
                        key: deepcopy(row.get(key))
                        for key in (
                            "release_time_utc",
                            "release_timestamp_taipei_date",
                            "release_date_conflict",
                            "release_store_date",
                            "release_date_normalization",
                            "release_display_provider",
                            "release_date_verified_at",
                        )
                    }
                    if taiwan_authority
                    else {}
                ),
            }
        )
    return result


def _reuse_discovery_mappings(
    state: dict,
    catalog: list[dict],
    discovery_state: dict | None,
    tracking_state: dict | None,
    clock: datetime,
    *,
    normalize_discovery_state,
    normalize_tracking_state,
    _qualified_members,
    NON_GAME_IDS,
    parse_timestamp,
    _id,
    deepcopy,
    METHOD,
) -> set[str]:
    """Reuse the same validated official ID chain without another API lookup.

    Reverse intake may know a link before the game enters the curated catalog.
    Only its current, qualified Twitch membership and a unique AppID identity
    can seed a forward cache. A conflicting or newer forward decision wins.
    """
    if discovery_state is None or tracking_state is None:
        return set()
    discovery = normalize_discovery_state(discovery_state)
    tracking = normalize_tracking_state(
        tracking_state, clock, non_game_ids=NON_GAME_IDS
    )
    qualified = _qualified_members(tracking, clock)
    source = discovery.get("steam_source_id")
    if not source or state.get("steam_source_id") not in (None, source):
        return set()
    public_ids = {steam["steam_appid"] for steam in catalog}
    candidates: dict[str, dict[tuple[str, str], tuple[dict, dict]]] = {}
    for twitch_id, row in discovery["games"].items():
        if (
            twitch_id not in qualified
            or row.get("active") is not True
            or row.get("status") != "matched"
            or (parse_timestamp(row["checked_at"]) > clock)
            or (parse_timestamp(row["updated_at"]) > clock)
        ):
            continue
        entry, enrollment = qualified[twitch_id]
        current_igdb = entry.get("igdb_id") or (
            entry.get("last_observation") or {}
        ).get("igdb_id")
        try:
            same_igdb = _id(current_igdb) == row["igdb_id"]
        except (ValueError, TypeError):
            same_igdb = False
        if not same_igdb or row["twitch_enrollment"] != enrollment:
            continue
        for appid in set(row["steam_appids"]) & public_ids:
            candidates.setdefault(appid, {})[twitch_id, row["igdb_id"]] = (row, entry)
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
            if previous.get("checked_at") and parse_timestamp(
                previous["checked_at"]
            ) >= parse_timestamp(row["checked_at"]):
                continue
        cached = state["games"].setdefault(appid, {"steam_appid": appid})
        for key in ("candidates",):
            cached.pop(key, None)
        cached.update(
            status="matched",
            method=METHOD,
            igdb_id=igdb_id,
            twitch_game_id=twitch_id,
            twitch_name=row["twitch_name"],
            box_art_url=entry.get("box_art_url"),
            checked_at=row["checked_at"],
            retry_at=None,
            reason="authoritative_id_chain",
            identity_source=row["method"],
            discovery_source_updated_at=discovery.get("updated_at"),
            discovery_links=deepcopy(
                [link for link in row["links"] if link["steam_appid"] == appid]
            ),
        )
        reused.add(appid)
    if reused:
        state["steam_source_id"] = source
    return reused
