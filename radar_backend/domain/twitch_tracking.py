"""Pure Twitch and Steam tracking memberships with explicit composition ports."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta

from radar_backend.domain.twitch_newness import parse_timestamp, timestamp

TRACKING_DAYS = 30
STATUSES = {"active", "expired", "excluded"}


def _sources(entry: dict, *, deepcopy_fn=None) -> dict:
    """Migrate the old single-source registry without rewriting its evidence."""
    deepcopy_fn = deepcopy if deepcopy_fn is None else deepcopy_fn
    if "tracking_sources" not in entry:
        entry["tracking_sources"] = {
            "twitch_new": {
                key: deepcopy_fn(entry.get(key))
                for key in (
                    "first_seen_at",
                    "last_seen_at",
                    "updated_at",
                    "release_at",
                    "release_source",
                    "expires_at",
                    "status",
                    "status_reason",
                    "enrollment",
                )
            }
        }
        entry["tracking_sources"]["twitch_new"]["source"] = "twitch_new"
    sources = entry["tracking_sources"]
    if not isinstance(sources, dict) or not sources:
        raise ValueError("Tracked game requires tracking sources")
    return sources


def normalize_tracking_state(
    payload: dict | None,
    now: datetime | None = None,
    *,
    non_game_ids=(),
    deepcopy_fn=None,
    timestamp_fn=None,
    parse_timestamp_fn=None,
    sources_fn=None,
    reconcile_tracking_entry_fn=None,
    statuses=None,
) -> dict:
    """Validate and copy entries; migrate legacy evidence and reconcile expiry."""
    deepcopy_fn = deepcopy if deepcopy_fn is None else deepcopy_fn
    timestamp_fn = timestamp if timestamp_fn is None else timestamp_fn
    parse_timestamp_fn = parse_timestamp if parse_timestamp_fn is None else parse_timestamp_fn
    sources_fn = _sources if sources_fn is None else sources_fn
    reconcile_tracking_entry_fn = (
        reconcile_tracking_entry
        if reconcile_tracking_entry_fn is None
        else reconcile_tracking_entry_fn
    )
    statuses = STATUSES if statuses is None else statuses
    if payload is None:
        return {"schema_version": 1, "updated_at": timestamp_fn(now) if now else None, "games": {}}
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or (not isinstance(payload.get("games"), dict))
    ):
        raise ValueError("Invalid Twitch tracking state")
    state = deepcopy_fn(payload)
    if state.get("updated_at") is None and state["games"]:
        raise ValueError("Tracking state with games requires an update timestamp")
    if state.get("updated_at") is not None:
        parse_timestamp_fn(state["updated_at"])
    for game_id, entry in state["games"].items():
        if not isinstance(game_id, str) or not game_id.isdigit() or (not isinstance(entry, dict)):
            raise ValueError("Invalid tracked game ID or entry")
        if entry.get("game_id") != game_id or entry.get("status") not in statuses:
            raise ValueError("Invalid tracked game identity or status")
        if not isinstance(entry.get("game_name"), str) or not entry["game_name"]:
            raise ValueError("Tracked game requires a name")
        parse_timestamp_fn(entry.get("first_seen_at"))
        enrollment = entry.get("enrollment")
        if not isinstance(enrollment, dict) or not enrollment.get("source"):
            raise ValueError("Tracked game requires enrollment evidence")
        parse_timestamp_fn(enrollment.get("observed_at"))
        for field in ("last_seen_at", "updated_at", "release_at", "expires_at"):
            if entry.get(field) is not None:
                parse_timestamp_fn(entry[field])
        for key, source in sources_fn(entry).items():
            if key != "twitch_new" and (not (key.startswith("steam:") and key[6:].isdigit())):
                raise ValueError("Invalid tracking source identity")
            if not isinstance(source, dict) or source.get("status") not in statuses:
                raise ValueError("Invalid tracking source status")
            if key.startswith("steam:") and str(source.get("steam_appid")) != key[6:]:
                raise ValueError("Steam tracking source identity does not match")
            for field in (
                "first_seen_at",
                "last_seen_at",
                "updated_at",
                "release_at",
                "expires_at",
            ):
                if source.get(field) is not None:
                    parse_timestamp_fn(source[field])
        if entry.get("last_observation") is not None and (
            not isinstance(entry["last_observation"], dict)
        ):
            raise ValueError("Invalid tracked game observation")
        if entry.get("last_observation") is not None:
            observation = entry["last_observation"]
            if str(observation.get("game_id")) != game_id:
                raise ValueError("Tracked observation identity does not match")
            parse_timestamp_fn(observation.get("observation_at"))
        if now is not None:
            reconcile_tracking_entry_fn(entry, now, non_game_ids=non_game_ids)
    return state


def release_from_observation(
    row: dict,
    *,
    timestamp_fn=None,
    parse_timestamp_fn=None,
) -> tuple[str | None, str | None]:
    """Release cache age controls admission, not a known membership's expiry."""
    timestamp_fn = timestamp if timestamp_fn is None else timestamp_fn
    parse_timestamp_fn = parse_timestamp if parse_timestamp_fn is None else parse_timestamp_fn
    evidence = row.get("release_evidence") or {}
    experiments = row.get("release_experiment") or {}
    options = [
        (evidence.get("first_release_date"), "igdb_first_release_date"),
        (
            (experiments.get("igdb_first_release_date") or {}).get("release_at"),
            "igdb_first_release_date",
        ),
        (
            (experiments.get("twitch_original_release_date") or {}).get("release_at"),
            "twitch_original_release_date",
        ),
    ]
    for value, source in options:
        if value:
            try:
                return (timestamp_fn(parse_timestamp_fn(value)), source)
            except (ValueError, TypeError):
                continue
    return (None, None)


def reconcile_tracking_entry(
    entry: dict,
    now: datetime,
    *,
    release_at: str | None = None,
    release_source: str | None = None,
    non_game_ids=(),
    deepcopy_fn=None,
    timestamp_fn=None,
    parse_timestamp_fn=None,
    sources_fn=None,
    tracking_days=None,
    timedelta_type=None,
) -> dict:
    deepcopy_fn = deepcopy if deepcopy_fn is None else deepcopy_fn
    timestamp_fn = timestamp if timestamp_fn is None else timestamp_fn
    parse_timestamp_fn = parse_timestamp if parse_timestamp_fn is None else parse_timestamp_fn
    sources_fn = _sources if sources_fn is None else sources_fn
    tracking_days = TRACKING_DAYS if tracking_days is None else tracking_days
    timedelta_type = timedelta if timedelta_type is None else timedelta_type
    sources = sources_fn(entry)
    twitch = sources.get("twitch_new")
    if (
        twitch is not None
        and release_at
        and (
            not (
                twitch.get("release_at")
                and twitch.get("release_source") == "igdb_first_release_date"
                and (release_source == "twitch_original_release_date")
            )
        )
    ):
        twitch.update(
            release_at=timestamp_fn(parse_timestamp_fn(release_at)),
            release_source=release_source,
            updated_at=timestamp_fn(now),
        )
    permanently_excluded = entry["game_id"] in non_game_ids or entry.get("status") == "excluded"
    for key, source in sources.items():
        released = source.get("release_at")
        source["expires_at"] = (
            timestamp_fn(parse_timestamp_fn(released) + timedelta_type(days=tracking_days))
            if released
            else None
        )
        if permanently_excluded:
            source.update(
                status="excluded",
                status_reason=(
                    "non_game_category"
                    if entry["game_id"] in non_game_ids
                    else source.get("status_reason", "explicit_exclusion")
                ),
            )
        elif source.get("status") == "excluded":
            continue
        elif source["expires_at"] and now >= parse_timestamp_fn(source["expires_at"]):
            source.update(status="expired", status_reason="release_window_elapsed")
        elif key.startswith("steam:") and (not released or now < parse_timestamp_fn(released)):
            source.update(status="expired", status_reason="steam_not_released")
        else:
            source.update(
                status="active",
                status_reason=(
                    "steam_recent_release"
                    if key.startswith("steam:")
                    else "within_release_window" if released else "awaiting_release_date"
                ),
            )
    active = [source for source in sources.values() if source["status"] == "active"]
    matches = {
        str(steam.get("steam_appid")): deepcopy_fn(steam)
        for steam in entry.get("steam_matches", [])
    }
    for source in active:
        if source.get("steam"):
            matches[str(source.get("steam_appid"))] = deepcopy_fn(source["steam"])
    entry["steam_matches"] = list(matches.values())
    preferred = twitch or next(iter(sources.values()))
    entry["release_at"], entry["release_source"] = (
        preferred.get("release_at"),
        preferred.get("release_source"),
    )
    entry["expires_at"] = (
        None
        if any((not source.get("expires_at") for source in active))
        else max((source["expires_at"] for source in active), key=parse_timestamp_fn, default=None)
    )
    if permanently_excluded:
        entry.update(
            status="excluded",
            status_reason=(
                "non_game_category"
                if entry["game_id"] in non_game_ids
                else entry.get("status_reason", "explicit_exclusion")
            ),
        )
    elif active:
        entry.update(
            status="active",
            status_reason=(
                active[0]["status_reason"] if len(active) == 1 else "active_tracking_sources"
            ),
        )
    else:
        entry.update(status="expired", status_reason="release_window_elapsed")
        entry["expires_at"] = max(
            (source["expires_at"] for source in sources.values() if source.get("expires_at")),
            key=parse_timestamp_fn,
            default=None,
        )
    return entry


def admission_evidence(
    row: dict,
    observed_at: datetime,
    *,
    parse_timestamp_fn=None,
    release_from_observation_fn=None,
    tracking_days=None,
    timedelta_type=None,
) -> str | None:
    """Replay labelled Twitch evidence, including former 14-day IGDB records."""
    parse_timestamp_fn = parse_timestamp if parse_timestamp_fn is None else parse_timestamp_fn
    release_from_observation_fn = (
        release_from_observation
        if release_from_observation_fn is None
        else release_from_observation_fn
    )
    tracking_days = TRACKING_DAYS if tracking_days is None else tracking_days
    timedelta_type = timedelta if timedelta_type is None else timedelta_type
    verification = row.get("verification") or {}
    if verification.get("status") == "not_new":
        return None
    release_at, source = release_from_observation_fn(row)
    if source == "igdb_first_release_date":
        trial = (row.get("release_experiment") or {}).get(source) or {}
        evidence = row.get("release_evidence") or {}
        metadata_at = evidence.get("checked_at") or trial.get("metadata_observed_at")
        try:
            fresh = metadata_at and parse_timestamp_fn(
                metadata_at
            ) <= observed_at < parse_timestamp_fn(metadata_at) + timedelta_type(hours=24)
            if fresh and observed_at - parse_timestamp_fn(release_at) < timedelta_type(
                days=tracking_days
            ):
                return source
        except (ValueError, TypeError):
            pass
    if verification.get("status") == "new":
        try:
            if (
                parse_timestamp_fn(verification["observed_at"])
                <= observed_at
                < parse_timestamp_fn(verification["expires_at"])
            ):
                return "twitch_directory_dom"
        except (ValueError, TypeError, KeyError):
            pass
    trial = (row.get("release_experiment") or {}).get("twitch_original_release_date") or {}
    if trial.get("status") == "evaluated" and trial.get("predicted_new") is True:
        return "twitch_original_release_date"
    return None


def tracking_metadata(entry: dict, *, deepcopy_fn=None) -> dict:
    deepcopy_fn = deepcopy if deepcopy_fn is None else deepcopy_fn
    return {
        key: deepcopy_fn(entry.get(key))
        for key in (
            "status",
            "status_reason",
            "first_seen_at",
            "last_seen_at",
            "release_at",
            "release_source",
            "expires_at",
            "enrollment",
            "tracking_sources",
            "steam_matches",
        )
    }


def enroll_steam_mapping(
    state: dict,
    mapping: dict,
    now: datetime,
    *,
    non_game_ids=(),
    deepcopy_fn=None,
    timestamp_fn=None,
    parse_timestamp_fn=None,
    sources_fn=None,
    reconcile_tracking_entry_fn=None,
    tracking_days=None,
    timedelta_type=None,
) -> dict | None:
    """Enroll one confirmed Steam release without requiring live viewers."""
    deepcopy_fn = deepcopy if deepcopy_fn is None else deepcopy_fn
    timestamp_fn = timestamp if timestamp_fn is None else timestamp_fn
    parse_timestamp_fn = parse_timestamp if parse_timestamp_fn is None else parse_timestamp_fn
    sources_fn = _sources if sources_fn is None else sources_fn
    reconcile_tracking_entry_fn = (
        reconcile_tracking_entry
        if reconcile_tracking_entry_fn is None
        else reconcile_tracking_entry_fn
    )
    tracking_days = TRACKING_DAYS if tracking_days is None else tracking_days
    timedelta_type = timedelta if timedelta_type is None else timedelta_type
    game_id, appid = (
        str(mapping.get("twitch_game_id") or ""),
        str(mapping.get("steam_appid") or ""),
    )
    steam = deepcopy_fn(mapping.get("steam") or {})
    if (
        mapping.get("status") != "matched"
        or not game_id.isdigit()
        or (not appid.isdigit())
        or (game_id in non_game_ids)
    ):
        return None
    released = steam.get("release_at")
    if not released:
        return None
    release_time = parse_timestamp_fn(released)
    if not release_time <= now < release_time + timedelta_type(days=tracking_days):
        return None
    entry = state["games"].get(game_id)
    if entry is not None and entry["status"] == "excluded":
        return None
    enrollment = {
        "source": "steam_recent_release",
        "observed_at": timestamp_fn(now),
        "steam_appid": appid,
        "release_at": timestamp_fn(release_time),
        "release_source": "steam_taiwan_store",
    }
    if entry is None:
        entry = state["games"][game_id] = {
            "game_id": game_id,
            "game_name": mapping.get("twitch_name")
            or steam.get("display_name")
            or steam.get("name")
            or game_id,
            "igdb_id": mapping.get("igdb_id"),
            "box_art_url": mapping.get("box_art_url"),
            "first_seen_at": timestamp_fn(now),
            "updated_at": timestamp_fn(now),
            "status": "active",
            "enrollment": deepcopy_fn(enrollment),
            "tracking_sources": {},
        }
    sources = entry["tracking_sources"] if "tracking_sources" in entry else sources_fn(entry)
    key = "steam:" + appid
    previous = sources.get(key) or {}
    sources[key] = {
        "source": "steam_recent_release",
        "steam_appid": appid,
        "steam": steam,
        "release_at": timestamp_fn(release_time),
        "release_source": "steam_taiwan_store",
        "first_seen_at": previous.get("first_seen_at") or timestamp_fn(now),
        "enrollment": deepcopy_fn(previous.get("enrollment") or enrollment),
        "updated_at": timestamp_fn(now),
        "status": "active",
        "status_reason": "steam_recent_release",
    }
    for source_key, target_key in (
        ("twitch_name", "game_name"),
        ("igdb_id", "igdb_id"),
        ("box_art_url", "box_art_url"),
    ):
        if mapping.get(source_key):
            entry[target_key] = mapping[source_key]
    reconcile_tracking_entry_fn(entry, now, non_game_ids=non_game_ids)
    state["updated_at"] = timestamp_fn(now)
    return entry


def reconcile_steam_catalog(
    state: dict,
    catalog: list[dict],
    mappings: dict,
    now: datetime,
    *,
    non_game_ids=(),
    deepcopy_fn=None,
    timestamp_fn=None,
    sources_fn=None,
    reconcile_tracking_entry_fn=None,
    enroll_steam_mapping_fn=None,
) -> None:
    """Reconcile only a successfully loaded catalog; failures never remove it."""
    deepcopy_fn = deepcopy if deepcopy_fn is None else deepcopy_fn
    timestamp_fn = timestamp if timestamp_fn is None else timestamp_fn
    sources_fn = _sources if sources_fn is None else sources_fn
    reconcile_tracking_entry_fn = (
        reconcile_tracking_entry
        if reconcile_tracking_entry_fn is None
        else reconcile_tracking_entry_fn
    )
    enroll_steam_mapping_fn = (
        enroll_steam_mapping if enroll_steam_mapping_fn is None else enroll_steam_mapping_fn
    )
    by_appid = {str(steam["steam_appid"]): steam for steam in catalog}
    matches_by_game = {}
    for appid, mapping in (mappings.get("games") or {}).items():
        if mapping.get("status") != "matched" or appid not in by_appid:
            continue
        game_id = str(mapping.get("twitch_game_id") or "")
        if not game_id.isdigit() or game_id in non_game_ids:
            continue
        steam = deepcopy_fn(by_appid[appid])
        steam["mapping_checked_at"] = mapping.get("checked_at")
        matches_by_game.setdefault(game_id, []).append(steam)
        effective = {**mapping, "steam": steam}
        enroll_steam_mapping_fn(state, effective, now, non_game_ids=non_game_ids)
    for game_id, entry in state["games"].items():
        entry["steam_matches"] = matches_by_game.get(game_id, [])
        for key, source in sources_fn(entry).items():
            if not key.startswith("steam:"):
                continue
            appid = key[6:]
            steam = by_appid.get(appid)
            mapping = (mappings.get("games") or {}).get(appid) or {}
            if steam is None:
                source.update(
                    status="excluded",
                    status_reason="steam_not_in_catalog",
                    updated_at=timestamp_fn(now),
                )
            elif (
                mapping.get("status") == "matched" and str(mapping.get("twitch_game_id")) != game_id
            ):
                source.update(
                    status="excluded",
                    status_reason="steam_mapping_changed",
                    updated_at=timestamp_fn(now),
                )
            else:
                source.update(
                    steam=deepcopy_fn(steam),
                    release_at=steam.get("release_at"),
                    release_source="steam_taiwan_store",
                    status="active",
                    updated_at=timestamp_fn(now),
                )
        reconcile_tracking_entry_fn(entry, now, non_game_ids=non_game_ids)
    state["updated_at"] = timestamp_fn(now)


def enroll_observation(
    state: dict,
    row: dict,
    observed_at: datetime,
    *,
    min_viewers: int = 7000,
    non_game_ids=(),
    eligibility_at: datetime | None = None,
    allow_twitch_enrollment: bool = True,
    deepcopy_fn=None,
    timestamp_fn=None,
    sources_fn=None,
    reconcile_tracking_entry_fn=None,
    release_from_observation_fn=None,
    admission_evidence_fn=None,
    tracking_metadata_fn=None,
) -> dict | None:
    """Add qualified Twitch membership, then store one real census observation."""
    deepcopy_fn = deepcopy if deepcopy_fn is None else deepcopy_fn
    timestamp_fn = timestamp if timestamp_fn is None else timestamp_fn
    sources_fn = _sources if sources_fn is None else sources_fn
    reconcile_tracking_entry_fn = (
        reconcile_tracking_entry
        if reconcile_tracking_entry_fn is None
        else reconcile_tracking_entry_fn
    )
    release_from_observation_fn = (
        release_from_observation
        if release_from_observation_fn is None
        else release_from_observation_fn
    )
    admission_evidence_fn = (
        admission_evidence if admission_evidence_fn is None else admission_evidence_fn
    )
    tracking_metadata_fn = (
        tracking_metadata if tracking_metadata_fn is None else tracking_metadata_fn
    )
    game_id = str(row.get("game_id") or "")
    if not game_id.isdigit() or game_id in non_game_ids:
        if game_id in state["games"]:
            reconcile_tracking_entry_fn(
                state["games"][game_id], observed_at, non_game_ids=non_game_ids
            )
        return None
    entry = state["games"].get(game_id)
    eligible_time = eligibility_at or observed_at
    count = row.get("viewer_count")
    evidence_source = admission_evidence_fn(row, eligible_time)
    qualifies = (
        allow_twitch_enrollment
        and type(count) in (int, float)
        and (count >= min_viewers)
        and bool(evidence_source)
    )
    if entry is None:
        if not qualifies:
            return None
        entry = state["games"][game_id] = {
            "game_id": game_id,
            "game_name": row["game_name"],
            "first_seen_at": timestamp_fn(observed_at),
            "status": "active",
            "tracking_sources": {},
            "enrollment": {
                "source": evidence_source,
                "observed_at": timestamp_fn(observed_at),
                "viewer_count": count,
                "min_viewers": min_viewers,
            },
        }
    if entry["status"] == "excluded":
        return None
    sources = entry["tracking_sources"] if "tracking_sources" in entry else sources_fn(entry)
    if "twitch_new" not in sources and qualifies:
        sources["twitch_new"] = {
            "source": "twitch_new",
            "first_seen_at": timestamp_fn(observed_at),
            "enrollment": {
                "source": evidence_source,
                "observed_at": timestamp_fn(observed_at),
                "viewer_count": count,
                "min_viewers": min_viewers,
            },
            "status": "active",
            "status_reason": "awaiting_release_date",
        }
    for key in ("game_name", "igdb_id", "box_art_url"):
        if row.get(key):
            entry[key] = row[key]
    if "steam_matches" in row:
        entry["steam_matches"] = deepcopy_fn(row["steam_matches"])
    released, source = release_from_observation_fn(row)
    reconcile_tracking_entry_fn(
        entry, eligible_time, release_at=released, release_source=source, non_game_ids=non_game_ids
    )
    entry["last_seen_at"], entry["updated_at"] = (
        timestamp_fn(observed_at),
        timestamp_fn(observed_at),
    )
    if "twitch_new" in sources:
        sources["twitch_new"].update(
            last_seen_at=timestamp_fn(observed_at), updated_at=timestamp_fn(observed_at)
        )
    row["steam_matches"] = deepcopy_fn(entry.get("steam_matches") or [])
    row["tracking"] = tracking_metadata_fn(entry)
    row["observation_at"] = timestamp_fn(observed_at)
    row["observation_status"] = "current"
    row["observation_freshness"] = "fresh"
    entry["last_observation"] = deepcopy_fn(row)
    state["updated_at"] = timestamp_fn(observed_at)
    return entry
