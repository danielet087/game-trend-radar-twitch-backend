"""Pure Twitch and Steam tracking memberships with explicit composition ports."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta

from radar_backend.domain.twitch_audience import CHINESE_DISCOVERY_MIN_VIEWERS
from radar_backend.domain.twitch_newness import parse_timestamp, timestamp

TRACKING_DAYS = 30
# Steam games are followed from two weeks before their Steam release, so
# streams from deluxe-edition early access or early keys are on record.
PRE_RELEASE_DAYS = 14
STATUSES = {"active", "expired", "excluded"}
# Every launch is followed on its own: an Early Access start and the full
# release that may come years later each get PRE_RELEASE_DAYS before to
# TRACKING_DAYS after. Between launches nothing is recorded, except that a
# Twitch audience of the admission threshold outside the Twitch source's own
# launch windows (a demo, a beta, a game waiting for its full release, a game
# with no known date) keeps the game for HIT_DAYS after its latest such hour.
HIT_DAYS = 30
# An ended game is waiting for a launch under these reasons; reaching the
# viewer threshold again brings it back without fresh newness evidence.
AWAITING_REASONS = {"awaiting_full_release", "awaiting_launch", "steam_not_released"}
TIME_FIELDS = ("first_seen_at", "last_seen_at", "updated_at", "release_at", "expires_at",
               "resumes_at", "hit_started_at", "hit_until")


def _moment(value, parse_timestamp_fn=None):
    parse_timestamp_fn = parse_timestamp if parse_timestamp_fn is None else parse_timestamp_fn
    if not value:
        return None
    try:
        return parse_timestamp_fn(value)
    except (ValueError, TypeError):
        return None


def launch_times(key: str, source: dict, *, parse_timestamp_fn=None) -> list[datetime]:
    """The exact launches one source knows: its release, an Early Access start
    and the full release after it. IGDB dates count only when known to the day."""
    found = []
    released = _moment(source.get("release_at"), parse_timestamp_fn)
    if key.startswith("steam:"):
        if released:
            found.append(released)
        access = source.get("early_access") if isinstance(source.get("early_access"), dict) else {}
        found += [_moment(access.get(field), parse_timestamp_fn) for field in ("started_at", "full_release_at")]
    else:
        # Records from before precision was kept were taken as exact.
        if released and source.get("release_precision", "day") == "day":
            found.append(released)
        dates = source.get("launches") if isinstance(source.get("launches"), dict) else {}
        found += [_moment(dates.get(field), parse_timestamp_fn) for field in ("early_access_at", "full_release_at")]
    return sorted({moment for moment in found if moment})


def launch_windows(entry: dict, *, sources_fn=None, parse_timestamp_fn=None, timedelta_type=None,
                   tracking_days=None, pre_release_days=None) -> list[tuple]:
    """(start, end, launch, source key) for each launch of each source."""
    sources_fn = _sources if sources_fn is None else sources_fn
    timedelta_type = timedelta if timedelta_type is None else timedelta_type
    tracking_days = TRACKING_DAYS if tracking_days is None else tracking_days
    pre_release_days = PRE_RELEASE_DAYS if pre_release_days is None else pre_release_days
    windows = []
    for key, source in sources_fn(entry).items():
        if not isinstance(source, dict) or source.get("status") == "excluded":
            continue
        for launch in launch_times(key, source, parse_timestamp_fn=parse_timestamp_fn):
            windows.append((launch - timedelta_type(days=pre_release_days),
                            launch + timedelta_type(days=tracking_days), launch, key))
    return windows


def hit_counts(sources: dict, at: datetime, *, parse_timestamp_fn=None, timedelta_type=None,
               tracking_days=None, pre_release_days=None) -> bool:
    """Does a threshold audience at ``at`` keep the game on its own?

    Inside a launch window of the Twitch source the launch decides. Outside
    one it counts before a known launch (a demo, a beta), with no exact date,
    or while the game waits for a launch (Early Access before the full
    release); after its launches have passed it does not.
    """
    timedelta_type = timedelta if timedelta_type is None else timedelta_type
    tracking_days = TRACKING_DAYS if tracking_days is None else tracking_days
    pre_release_days = PRE_RELEASE_DAYS if pre_release_days is None else pre_release_days
    twitch = sources.get("twitch_new") if isinstance(sources.get("twitch_new"), dict) else {}
    launches = launch_times("twitch_new", twitch, parse_timestamp_fn=parse_timestamp_fn)
    if any(launch - timedelta_type(days=pre_release_days) <= at < launch + timedelta_type(days=tracking_days)
           for launch in launches):
        return False
    if not launches or any(at < launch for launch in launches):
        return True
    return any(
        isinstance(source, dict) and source.get("status") != "excluded"
        and _awaiting(key, source, launch_times(key, source, parse_timestamp_fn=parse_timestamp_fn), at,
                      parse_timestamp_fn=parse_timestamp_fn, timedelta_type=timedelta_type,
                      tracking_days=tracking_days)
        for key, source in sources.items()
    )


def hit_windows(source: dict, sources: dict, *, parse_timestamp_fn=None, timedelta_type=None,
                hit_days=None) -> list[tuple]:
    """The viewer-threshold windows of the Twitch source: its admission, when
    that counted on its own, and its latest recorded hits."""
    timedelta_type = timedelta if timedelta_type is None else timedelta_type
    hit_days = HIT_DAYS if hit_days is None else hit_days
    windows = []
    observed = _moment((source.get("enrollment") or {}).get("observed_at"), parse_timestamp_fn)
    if observed and hit_counts(sources, observed, parse_timestamp_fn=parse_timestamp_fn,
                               timedelta_type=timedelta_type):
        windows.append((observed, observed + timedelta_type(days=hit_days)))
    start = _moment(source.get("hit_started_at"), parse_timestamp_fn)
    until = _moment(source.get("hit_until"), parse_timestamp_fn)
    if start and until and start < until:
        windows.append((start, until))
    return windows


def _union(windows: list[tuple]) -> list[list]:
    merged = []
    for start, end in sorted((window[0], window[1]) for window in windows):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def _igdb_early_access(source: dict) -> bool:
    dates = source.get("launches") if isinstance(source.get("launches"), dict) else {}
    return bool(dates.get("early_access_at")) and not dates.get("full_release_at")


def _awaiting(key: str, source: dict, launches: list[datetime], now: datetime, *,
              parse_timestamp_fn=None, timedelta_type=None, tracking_days=None) -> bool:
    """Is a launch of this source still to come (known or not)?"""
    timedelta_type = timedelta if timedelta_type is None else timedelta_type
    tracking_days = TRACKING_DAYS if tracking_days is None else tracking_days
    if any(now < launch for launch in launches):
        return True
    if key.startswith("steam:"):
        access = source.get("early_access") if isinstance(source.get("early_access"), dict) else {}
        return access.get("active") is True and not access.get("full_release_at")
    if _igdb_early_access(source):
        return True
    released = _moment(source.get("release_at"), parse_timestamp_fn)
    if released is None:
        return True
    return (source.get("release_precision", "day") != "day"
            and now < released + timedelta_type(days=tracking_days))


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
        for field in ("last_seen_at", "updated_at", "release_at", "expires_at", "resumes_at"):
            if entry.get(field) is not None:
                parse_timestamp_fn(entry[field])
        for key, source in sources_fn(entry).items():
            if key != "twitch_new" and (not (key.startswith("steam:") and key[6:].isdigit())):
                raise ValueError("Invalid tracking source identity")
            if not isinstance(source, dict) or source.get("status") not in statuses:
                raise ValueError("Invalid tracking source status")
            if key.startswith("steam:") and str(source.get("steam_appid")) != key[6:]:
                raise ValueError("Steam tracking source identity does not match")
            for field in TIME_FIELDS:
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
            # Older observations carry a copy of their own entry's tracking
            # state. The entry is the current copy; the nested one only goes stale.
            observation.pop("tracking", None)
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


def launch_evidence(row: dict) -> tuple[str | None, dict | None]:
    """IGDB's precision of the first release date and its exact Early Access
    and full release dates, from the census row's release evidence."""
    evidence = row.get("release_evidence") if isinstance(row.get("release_evidence"), dict) else {}
    if not evidence.get("first_release_date"):
        return (None, None)
    precision = evidence.get("release_precision")
    launches = {field: evidence.get(field) for field in ("early_access_at", "full_release_at")}
    return (precision if isinstance(precision, str) else None,
            launches if "release_precision" in evidence else None)


def record_hit(entry: dict, at: datetime, *, sources_fn=None, parse_timestamp_fn=None,
               timestamp_fn=None, timedelta_type=None, hit_days=None) -> bool:
    """A threshold audience that counts on its own (see hit_counts) keeps the
    game for HIT_DAYS after this hour."""
    sources_fn = _sources if sources_fn is None else sources_fn
    timestamp_fn = timestamp if timestamp_fn is None else timestamp_fn
    timedelta_type = timedelta if timedelta_type is None else timedelta_type
    hit_days = HIT_DAYS if hit_days is None else hit_days
    sources = sources_fn(entry)
    source = sources.get("twitch_new")
    if source is None or not hit_counts(sources, at, parse_timestamp_fn=parse_timestamp_fn,
                                        timedelta_type=timedelta_type):
        return False
    start = _moment(source.get("hit_started_at"), parse_timestamp_fn)
    until = _moment(source.get("hit_until"), parse_timestamp_fn)
    ongoing = bool(start and until and start <= at <= until)
    if not ongoing:
        source["hit_started_at"] = timestamp_fn(at)
    source["hit_until"] = timestamp_fn(max(until, at + timedelta_type(days=hit_days))
                                       if ongoing else at + timedelta_type(days=hit_days))
    source["updated_at"] = timestamp_fn(at)
    return True


def reconcile_tracking_entry(
    entry: dict,
    now: datetime,
    *,
    release_at: str | None = None,
    release_source: str | None = None,
    release_precision: str | None = None,
    launches: dict | None = None,
    non_game_ids=(),
    deepcopy_fn=None,
    timestamp_fn=None,
    parse_timestamp_fn=None,
    sources_fn=None,
    tracking_days=None,
    pre_release_days=None,
    timedelta_type=None,
) -> dict:
    """Each source is active inside one of its launch windows (or, for Twitch,
    its viewer-threshold window); otherwise it has ended or waits for a launch."""
    deepcopy_fn = deepcopy if deepcopy_fn is None else deepcopy_fn
    timestamp_fn = timestamp if timestamp_fn is None else timestamp_fn
    parse_timestamp_fn = parse_timestamp if parse_timestamp_fn is None else parse_timestamp_fn
    sources_fn = _sources if sources_fn is None else sources_fn
    tracking_days = TRACKING_DAYS if tracking_days is None else tracking_days
    pre_release_days = PRE_RELEASE_DAYS if pre_release_days is None else pre_release_days
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
        if release_source == "igdb_first_release_date":
            if release_precision is not None:
                twitch["release_precision"] = release_precision
            if launches is not None:
                twitch["launches"] = deepcopy_fn(launches)
        else:
            twitch["release_precision"] = "day"
    permanently_excluded = entry["game_id"] in non_game_ids or entry.get("status") == "excluded"
    windows = launch_windows(entry, sources_fn=sources_fn, parse_timestamp_fn=parse_timestamp_fn,
                             timedelta_type=timedelta_type, tracking_days=tracking_days,
                             pre_release_days=pre_release_days)
    for key, source in sources.items():
        own = [(begin, end, launch) for begin, end, launch, owner in windows if owner == key]
        if key == "twitch_new":
            own += [(begin, end, None) for begin, end in hit_windows(
                source, sources, parse_timestamp_fn=parse_timestamp_fn, timedelta_type=timedelta_type)]
        spans = _union(own)
        current = next((span for span in spans if span[0] <= now < span[1]), None)
        later = [span for span in spans if now < span[0]]
        ended = [span for span in spans if span[1] <= now]
        source["expires_at"] = (
            timestamp_fn(current[1]) if current
            else timestamp_fn(ended[-1][1]) if ended
            else timestamp_fn(later[0][1]) if later else None
        )
        if later and not current:
            source["resumes_at"] = timestamp_fn(later[0][0])
        else:
            source.pop("resumes_at", None)
        released = [launch for _, _, launch in own if launch is not None]
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
        elif current:
            ahead = [launch for begin, end, launch in own
                     if launch is not None and begin <= now < end]
            if key.startswith("steam:"):
                reason = (
                    "steam_recent_release" if not ahead or any(launch <= now for launch in ahead)
                    else "steam_full_release_pending" if any(launch <= now for launch in released)
                    else "steam_pre_release"
                )
            else:
                reason = "within_release_window" if ahead else "twitch_viewers"
            source.update(status="active", status_reason=reason)
        elif key.startswith("steam:") and not any(launch <= now for launch in released):
            source.update(status="expired", status_reason="steam_not_released")
        elif _awaiting(key, source, released, now, parse_timestamp_fn=parse_timestamp_fn,
                       timedelta_type=timedelta_type, tracking_days=tracking_days):
            source.update(
                status="expired",
                status_reason=(
                    "awaiting_full_release"
                    if key.startswith("steam:") or _igdb_early_access(source)
                    else "awaiting_launch"
                ),
            )
        else:
            source.update(status="expired", status_reason="release_window_elapsed")
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
    entry.pop("resumes_at", None)
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
        reasons = {source.get("status_reason") for source in sources.values()
                   if source.get("status") == "expired"}
        entry.update(
            status="expired",
            status_reason=next(
                (reason for reason in ("awaiting_full_release", "awaiting_launch", "steam_not_released")
                 if reason in reasons),
                "release_window_elapsed",
            ),
        )
        entry["expires_at"] = max(
            (source["expires_at"] for source in sources.values() if source.get("expires_at")),
            key=parse_timestamp_fn,
            default=None,
        )
        resumes = [source["resumes_at"] for source in sources.values()
                   if source.get("status") == "expired" and source.get("resumes_at")]
        if resumes:
            entry["resumes_at"] = min(resumes, key=parse_timestamp_fn)
    return entry


def awaiting_launch(entry: dict | None) -> bool:
    """An ended game that reaching the viewer threshold again brings back."""
    return (isinstance(entry, dict) and entry.get("status") == "expired"
            and entry.get("status_reason") in AWAITING_REASONS)


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


def prelaunch_only(entry: dict | None) -> bool:
    """Only Steam sources before their release keep this game tracked."""
    sources = (entry or {}).get("tracking_sources")
    active = [source for source in (sources.values() if isinstance(sources, dict) else [])
              if isinstance(source, dict) and source.get("status") == "active"]
    return bool(active) and all(source.get("status_reason") == "steam_pre_release" for source in active)


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
    pre_release_days=None,
    timedelta_type=None,
) -> dict | None:
    """Enroll one confirmed Steam game from two weeks before its release,
    without requiring live viewers."""
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
    pre_release_days = PRE_RELEASE_DAYS if pre_release_days is None else pre_release_days
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
    # The Steam release and, for an Early Access game, its start and full release.
    launches = launch_times("steam:" + appid, {"release_at": released, "early_access": steam.get("early_access")},
                            parse_timestamp_fn=parse_timestamp_fn)
    if not any(launch - timedelta_type(days=pre_release_days)
               <= now < launch + timedelta_type(days=tracking_days) for launch in launches):
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
        "early_access": deepcopy_fn(steam.get("early_access")),
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
                    early_access=deepcopy_fn(steam.get("early_access")),
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
    chinese_discovery: bool = False,
    revival: bool = False,
    deepcopy_fn=None,
    timestamp_fn=None,
    sources_fn=None,
    reconcile_tracking_entry_fn=None,
    release_from_observation_fn=None,
    admission_evidence_fn=None,
    tracking_metadata_fn=None,
    record_hit_fn=None,
) -> dict | None:
    """Add qualified Twitch membership, then store one real census observation.

    ``revival`` marks an ended game waiting for a launch: reaching the
    threshold brings it back without fresh newness evidence.
    """
    record_hit_fn = record_hit if record_hit_fn is None else record_hit_fn
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
    audience = row.get("audience_regions", {}).get("zh", {}) if chinese_discovery else row
    count = audience.get("viewer_count")
    minimum = CHINESE_DISCOVERY_MIN_VIEWERS if chinese_discovery else min_viewers
    evidence_source = admission_evidence_fn(row, eligible_time)
    qualifies = (
        allow_twitch_enrollment
        and type(count) in (int, float)
        and (count >= minimum)
        and bool(evidence_source)
    )
    revived = (
        revival
        and awaiting_launch(entry)
        and type(count) in (int, float)
        and count >= minimum
    )
    enrollment = {
        "source": evidence_source,
        "observed_at": timestamp_fn(observed_at),
        "viewer_count": count,
        "min_viewers": minimum,
    }
    if chinese_discovery:
        enrollment["audience_region"] = "zh"
    if entry is None:
        if not qualifies:
            return None
        entry = state["games"][game_id] = {
            "game_id": game_id,
            "game_name": row["game_name"],
            "first_seen_at": timestamp_fn(observed_at),
            "status": "active",
            "tracking_sources": {},
            "enrollment": enrollment.copy(),
        }
    if entry["status"] == "excluded":
        return None
    sources = entry["tracking_sources"] if "tracking_sources" in entry else sources_fn(entry)
    if "twitch_new" not in sources and (qualifies or revived):
        sources["twitch_new"] = {
            "source": "twitch_new",
            "first_seen_at": timestamp_fn(observed_at),
            "enrollment": enrollment if qualifies else {**enrollment, "source": "awaiting_launch"},
            "status": "active",
            "status_reason": "twitch_viewers",
        }
    for key in ("game_name", "igdb_id", "box_art_url"):
        if row.get(key):
            entry[key] = row[key]
    if "steam_matches" in row:
        entry["steam_matches"] = deepcopy_fn(row["steam_matches"])
    released, source = release_from_observation_fn(row)
    precision, launches = launch_evidence(row)
    reconcile_tracking_entry_fn(
        entry, eligible_time, release_at=released, release_source=source,
        release_precision=precision, launches=launches, non_game_ids=non_game_ids
    )
    if (qualifies or revived) and record_hit_fn(entry, eligible_time):
        reconcile_tracking_entry_fn(entry, eligible_time, non_game_ids=non_game_ids)
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
    # The published census row keeps its tracking state; the entry already is it.
    entry["last_observation"] = deepcopy_fn({key: value for key, value in row.items() if key != "tracking"})
    state["updated_at"] = timestamp_fn(observed_at)
    return entry
