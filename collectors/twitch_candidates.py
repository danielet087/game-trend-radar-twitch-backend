"""Threshold discovery and stream census, separate from NEW-badge verification.

Helix does not expose Twitch's NEW badge. Only dated, explicit observations
may confirm it. The IGDB 30-day collection filter never changes that verdict.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import logging
import math
from pathlib import Path
from statistics import median
import time
from typing import Any, Callable

import requests
from urllib3.util import Timeout

from collectors.twitch_live import CollectionDeadlineExceeded, TwitchClient
from collectors.twitch_audience import CACHE_PATH, FollowerResolver, attach_filtered_audience
from collectors.twitch_newness import (
    RELEASE_DATES_PATH, SOURCE_RULES, attach_experiments, evaluate_date,
    load_release_dates, parse_timestamp, timestamp,
)
from collectors.twitch_tracking import (
    enroll_observation, normalize_tracking_state, reconcile_tracking_entry, reconcile_steam_catalog,
)

LOGGER = logging.getLogger(__name__)
REGISTRY_PATH = Path(__file__).resolve().parents[1] / "data/twitch_category_verification.json"
IGDB_URL = "https://api.igdb.com/v4/games"
NON_GAME_IDS = {
    "509658": "Just Chatting", "509672": "IRL", "509663": "Special Events",
    "509659": "ASMR", "26936": "Music",
}


class IncompleteCollection(RuntimeError):
    """A partial enumeration must not replace the last usable snapshot."""


def load_verifications(path: str | Path = REGISTRY_PATH) -> dict[str, dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1 or not isinstance(payload.get("observations"), dict):
        raise ValueError("Invalid verification registry")
    observations = payload["observations"]
    for game_id, row in observations.items():
        if not game_id.isdigit() or not isinstance(row, dict):
            raise ValueError("Invalid verification game ID or record")
        if row.get("status") not in {"new", "not_new"}:
            raise ValueError("Verification status must be explicit")
        if row.get("source") != "twitch_directory_dom" or not row.get("source_url"):
            raise ValueError("Verification needs a direct Twitch directory observation")
        observed = parse_timestamp(row["observed_at"])
        expires = parse_timestamp(row["expires_at"])
        if not observed < expires <= observed + timedelta(hours=24):
            raise ValueError("Badge observations may be cached for at most 24 hours")
    return observations


def verification_for(game_id: str, observations: dict, now: datetime) -> dict:
    row = observations.get(game_id)
    if row is None:
        return {"status": "pending", "reason": "no_observation"}
    # A game absent from a partial directory is never treated as not_new.
    observed, expires = parse_timestamp(row["observed_at"]), parse_timestamp(row["expires_at"])
    if observed <= now < expires:
        return dict(row)
    return {
        "status": "pending", "reason": "observation_expired_or_future",
        "previous_status": row["status"], "observed_at": row["observed_at"],
        "expires_at": row["expires_at"], "source": row["source"], "source_url": row["source_url"],
    }


class PageReader:
    def __init__(self, client: TwitchClient, max_calls: int, *, deadline: float | None = None,
                 monotonic: Callable[[], float] = time.monotonic):
        self.client, self.max_calls, self.calls = client, max_calls, 0
        self.deadline, self.monotonic = deadline, monotonic

    def get(self, endpoint: str, params: dict) -> tuple[list[dict], str | None]:
        if self.deadline is not None and self.monotonic() >= self.deadline:
            raise IncompleteCollection("collection_deadline_exhausted during census; nothing will be published")
        if self.calls >= self.max_calls:
            raise IncompleteCollection("Helix call budget exhausted; nothing will be published")
        self.calls += 1
        try:
            payload = self.client.get(endpoint, params=params)
        except CollectionDeadlineExceeded:
            raise IncompleteCollection("collection_deadline_exhausted during census; nothing will be published") from None
        if self.deadline is not None and self.monotonic() >= self.deadline:
            raise IncompleteCollection("collection_deadline_exhausted during census; nothing will be published")
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise IncompleteCollection(f"Malformed Helix {endpoint} response")
        rows = payload["data"]
        pagination = payload.get("pagination", {})
        if not all(isinstance(row, dict) for row in rows) or not isinstance(pagination, dict):
            raise IncompleteCollection(f"Malformed Helix {endpoint} page")
        cursor = pagination.get("cursor")
        if cursor is not None and not isinstance(cursor, str):
            raise IncompleteCollection("Malformed pagination cursor")
        return rows, cursor or None


def category_metrics(reader: PageReader, game_id: str, max_pages: int = 150, *, retain_channels: bool = False) -> dict:
    channels: dict[str, dict] = {}
    seen_cursors: set[str] = set()
    after = None
    duplicates = 0
    started = timestamp(datetime.now(timezone.utc))
    for page_index in range(max_pages):
        params = {"game_id": game_id, "type": "live", "first": 100}
        if after:
            params["after"] = after
        streams, cursor = reader.get("streams", params)
        for stream in streams:
            # A changing category during pagination must not mix another game's viewers.
            if str(stream.get("game_id")) != game_id:
                raise IncompleteCollection(f"Category changed while enumerating game {game_id}")
            user_id, viewers = str(stream.get("user_id") or ""), stream.get("viewer_count")
            if not user_id or type(viewers) is not int or viewers < 0:
                raise IncompleteCollection(f"Invalid stream metrics for game {game_id}")
            if stream.get("type") != "live":
                raise IncompleteCollection(f"Non-live or invalid stream for game {game_id}")
            duplicates += int(user_id in channels)
            channels[user_id] = stream  # Latest observation per broadcaster, counted once.
        if not cursor:
            break
        if cursor in seen_cursors:
            raise IncompleteCollection(f"Repeated stream cursor for game {game_id}")
        seen_cursors.add(cursor)
        after = cursor
    else:
        raise IncompleteCollection(f"Stream page limit reached for game {game_id}")
    counts = [row["viewer_count"] for row in channels.values()]
    languages = Counter(str(row.get("language") or "other") for row in channels.values())
    result = {
        "viewer_count": sum(counts), "streamer_count": len(counts),
        "median_viewer_count": median(counts) if counts else None,
        "language_streamers": dict(languages.most_common()),
        "measurement_started_at": started, "measurement_finished_at": timestamp(datetime.now(timezone.utc)),
        "pagination_complete": True, "stream_pages": page_index + 1,
        "duplicate_broadcasters_removed": duplicates,
    }
    if retain_channels:
        # Private, in-memory input only. Removed before a candidate is assembled.
        result["_channels"] = list(channels.values())
    return result


def release_hints(client: TwitchClient, games: list[dict], now: datetime, *, deadline: float | None = None,
                  monotonic: Callable[[], float] = time.monotonic) -> tuple[dict, str]:
    ids = sorted({str(row.get("igdb_id")) for row in games if str(row.get("igdb_id") or "").isdigit()})
    if not ids:
        return {}, "no_igdb_ids"
    hints = {}
    # One batch is at most 100 IDs. Dates filter collection, never confirm NEW.
    for offset in range(0, len(ids), 100):
        query = "fields id,name,first_release_date; where id = (" + ",".join(ids[offset:offset + 100]) + "); limit 100;"
        try:
            if deadline is not None and monotonic() >= deadline:
                raise CollectionDeadlineExceeded("collection_deadline_exhausted")
            client._wait()
            remaining = deadline - monotonic() if deadline is not None else None
            if remaining is not None and remaining <= 0:
                raise CollectionDeadlineExceeded("collection_deadline_exhausted")
            try:
                response = client.session.post(
                    IGDB_URL, data=query,
                    headers={"Client-ID": client.client_id, "Authorization": f"Bearer {client.access_token}"},
                    timeout=(Timeout(total=remaining, connect=min(client.timeout_seconds, remaining),
                                     read=min(client.timeout_seconds, remaining))
                             if remaining is not None else client.timeout_seconds),
                )
            finally:
                client._last_request_at = monotonic()
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list):
                raise ValueError("Invalid IGDB response")
            for row in rows:
                if not isinstance(row, dict):
                    continue
                raw_date = row.get("first_release_date")
                date = datetime.fromtimestamp(raw_date, timezone.utc) if type(raw_date) in (int, float) else None
                age = now - date if date else None
                window = timedelta(days=SOURCE_RULES["igdb_first_release_date"]["window_days"])
                band = "unknown" if age is None else "upcoming" if age < timedelta(0) else "recent_release" if age < window else "older_release"
                hints[str(row["id"])] = {
                    "source": "IGDB", "checked_at": timestamp(now),
                    "first_release_date": timestamp(date) if date else None,
                    "release_band": band, "confirms_twitch_new_badge": False,
                }
        except CollectionDeadlineExceeded:
            LOGGER.warning("IGDB hints stopped at collection deadline; remaining metadata stays unknown")
            return hints, "collection_deadline_exhausted"
        except (requests.RequestException, ValueError, KeyError, OverflowError, OSError) as exc:
            # Never print response bodies/headers or turn metadata failures into exclusions.
            status = getattr(getattr(exc, "response", None), "status_code", None)
            LOGGER.warning("IGDB release hints unavailable (%s); verification stays pending", status or type(exc).__name__)
            return hints, "unavailable_or_partial"
    return hints, "ok"


def collect_candidates(
    *, client_id: str, client_secret: str, min_viewers: int = 7000,
    max_category_pages: int = 5, max_stream_pages: int = 150, max_api_calls: int = 1200,
    registry_path: str | Path = REGISTRY_PATH, include_release_hints: bool = True,
    release_dates_path: str | Path = RELEASE_DATES_PATH,
    client: TwitchClient | None = None, now: datetime | None = None, category_page_size: int = 100,
    include_filtered_audience: bool = False, followers_cache_path: str | Path = CACHE_PATH,
    followers_max_calls: int | None = None, followers_max_seconds: float | None = None,
    max_collection_seconds: float = 1500,
    monotonic: Callable[[], float] = time.monotonic,
    tracking_state: dict | None = None,
    steam_catalog: dict | None = None, steam_mapping_state: dict | None = None,
) -> dict[str, Any]:
    collection_started = monotonic()
    if not math.isfinite(max_collection_seconds) or max_collection_seconds <= 0:
        raise ValueError("Collection time budget must be positive and finite")
    deadline = collection_started + max_collection_seconds
    if min(min_viewers, max_category_pages, max_stream_pages, max_api_calls) < 1 or not 1 <= category_page_size <= 100:
        raise ValueError("Threshold and limits must be positive; page size must be 1..100")
    clock = now or datetime.now(timezone.utc)
    tracking = normalize_tracking_state(tracking_state, clock, non_game_ids=NON_GAME_IDS)
    observations = load_verifications(registry_path)
    release_dates = load_release_dates(release_dates_path)
    client = client or TwitchClient(client_id, client_secret, request_interval=0.3)
    if isinstance(client, TwitchClient):
        client.collection_deadline, client.monotonic = deadline, monotonic
    reader = PageReader(client, max_api_calls, deadline=deadline, monotonic=monotonic)
    from collectors.steam_twitch_mapping import normalize_mapping_state, normalize_steam_catalog, refresh_mappings
    mappings = normalize_mapping_state(steam_mapping_state)
    steam_summary = {"status": "not_supplied", "catalog_games": None, "recent_releases": None}
    steam_matches_by_id = None
    if steam_catalog is not None:
        catalog = normalize_steam_catalog(steam_catalog, clock)
        mappings = refresh_mappings(client, steam_catalog, mappings, clock, deadline=deadline, monotonic=monotonic)
        reconcile_steam_catalog(tracking, catalog, mappings, clock, non_game_ids=NON_GAME_IDS)
        steam_by_appid = {steam["steam_appid"]: steam for steam in catalog}
        steam_matches_by_id = {}
        for appid, mapping in mappings["games"].items():
            if mapping.get("status") == "matched" and appid in steam_by_appid:
                steam_matches_by_id.setdefault(str(mapping["twitch_game_id"]), []).append(steam_by_appid[appid])
        steam_summary = {
            "status": "ok", "catalog_games": len(catalog),
            "recent_releases": sum(bool(steam.get("is_recent")) for steam in catalog),
            "matched_games": sum(row.get("status") == "matched" for row in mappings["games"].values()),
            "mapping_report": mappings.get("report"),
        }
    candidates_by_id, excluded_by_id = {}, {}
    retained_by_id = {}
    channels_by_game: dict[str, list[dict]] = {}
    seen_ids, seen_cursors = set(), set()
    measured_ids = set()
    below_ids = set()
    hints, requested_igdb_ids, hints_statuses = {}, set(), []
    igdb_predictions = {}
    after = None
    measured_count = below_count = 0
    stop_reason = None

    def refresh_tracking(game: dict, prediction: dict, at: datetime) -> dict | None:
        entry = tracking["games"].get(str(game["id"]))
        if entry is None:
            return None
        for source_key, target_key in (("name", "game_name"), ("igdb_id", "igdb_id"), ("box_art_url", "box_art_url")):
            if game.get(source_key):
                entry[target_key] = game[source_key]
        released = prediction.get("release_at") if prediction.get("status") == "evaluated" else None
        release_source = "igdb_first_release_date" if released else None
        if not released:
            dated = release_dates.get(str(game["id"]), {})
            trial = evaluate_date(dated.get("original_release_date"), dated.get("observed_at"), at,
                                  source="twitch_original_release_date")
            if trial.get("status") == "evaluated":
                released, release_source = trial.get("release_at"), "twitch_original_release_date"
        reconcile_tracking_entry(entry, at, release_at=released, release_source=release_source,
                                 non_game_ids=NON_GAME_IDS)
        entry["updated_at"] = timestamp(at)
        return entry

    for page_index in range(max_category_pages):
        params: dict[str, Any] = {"first": category_page_size}
        if after:
            params["after"] = after
        games, cursor = reader.get("games/top", params)
        if cursor and cursor in seen_cursors:
            raise IncompleteCollection("Repeated category cursor")
        # Validate and batch release metadata before any expensive stream census.
        # Missing/failed metadata remains unknown and cannot exclude a category.
        for game in games:
            if not str(game.get("id") or "").isdigit() or not isinstance(game.get("name"), str) or not game["name"]:
                raise IncompleteCollection("Invalid category metadata")
        hints_clock = now or datetime.now(timezone.utc)
        if include_release_hints:
            metadata_games = [game for game in games
                              if str(game["id"]) not in NON_GAME_IDS
                              and str(game.get("igdb_id") or "").isdigit()
                              and str(game["igdb_id"]) not in requested_igdb_ids]
            if metadata_games:
                page_hints, page_hints_status = release_hints(
                    client, metadata_games, hints_clock, deadline=deadline, monotonic=monotonic,
                )
                hints.update(page_hints)
                requested_igdb_ids.update(str(game["igdb_id"]) for game in metadata_games)
                hints_statuses.append(page_hints_status)
        page_measured, page_qualified, page_new_measured = 0, 0, 0
        page_seen = set()
        for game in games:
            game_id = str(game.get("id") or "")
            if game_id in page_seen:
                continue
            page_seen.add(game_id)
            previously_seen = game_id in seen_ids
            seen_ids.add(game_id)
            if previously_seen:
                # Ranking can repeat across pages. A confirmed ID has one
                # census per run, shared by every admission source.
                continue
            verification = verification_for(game_id, observations, now or datetime.now(timezone.utc))
            hint = hints.get(str(game.get("igdb_id")), {})
            prediction = evaluate_date(hint.get("first_release_date"), hint.get("checked_at"), hints_clock,
                                       source="igdb_first_release_date")
            tracked_entry = refresh_tracking(game, prediction, hints_clock)
            already_tracking = tracked_entry is not None and tracked_entry["status"] == "active"
            if game_id not in NON_GAME_IDS:
                igdb_predictions[game_id] = prediction
            if game_id in NON_GAME_IDS or (verification["status"] == "not_new" and not already_tracking):
                candidates_by_id.pop(game_id, None)
                channels_by_game.pop(game_id, None)
                below_ids.discard(game_id)
                excluded_by_id[game_id] = {
                    "game_id": game_id, "game_name": game["name"],
                    "igdb_id": game.get("igdb_id") or None,
                    "reason": "non_game_category" if game_id in NON_GAME_IDS else "observed_not_new",
                    "verification": verification, "metrics_collected": False, "viewer_threshold_met": None,
                }
                continue
            if not already_tracking and (prediction["status"] == "evaluated" and prediction["predicted_new"] is False
                    or tracked_entry is not None and tracked_entry["status"] != "active"):
                candidates_by_id.pop(game_id, None)
                channels_by_game.pop(game_id, None)
                below_ids.discard(game_id)
                excluded_by_id[game_id] = {
                    "game_id": game_id, "game_name": game["name"],
                    "igdb_id": game.get("igdb_id") or None,
                    "reason": "igdb_release_outside_window", "verification": verification,
                    "metrics_collected": False, "viewer_threshold_met": None,
                    "exclusion_source": "igdb_first_release_date",
                    "exclusion_window_days": 30,
                    "release_evidence": hint,
                }
                continue
            excluded_by_id.pop(game_id, None)
            # One census per ID also serves both Twitch and Steam membership.
            metrics = category_metrics(reader, game_id, max_stream_pages, retain_channels=include_filtered_audience)
            channels = metrics.pop("_channels", None)
            measured_count += 1
            measured_ids.add(game_id)
            page_measured += 1
            page_new_measured += int(not previously_seen)
            row = {
                "game_id": game_id, "game_name": game["name"],
                "box_art_url": game.get("box_art_url"), "igdb_id": game.get("igdb_id") or None,
                "verification": verification, **metrics,
            }
            if already_tracking:
                retained_by_id[game_id] = row
                if channels is not None:
                    channels_by_game[game_id] = channels
            if metrics["viewer_count"] < min_viewers:
                below_count += 1
                below_ids.add(game_id)
                candidates_by_id.pop(game_id, None)
                if not already_tracking:
                    channels_by_game.pop(game_id, None)
                continue
            page_qualified += 1
            below_ids.discard(game_id)
            if verification["status"] == "not_new" or (prediction["status"] == "evaluated" and prediction["predicted_new"] is False):
                # Steam keeps old global releases observable but never turns
                # them into Twitch new-admission discovery candidates.
                candidates_by_id.pop(game_id, None)
                continue
            candidates_by_id[game_id] = row
            if channels is not None:
                channels_by_game[game_id] = channels
        LOGGER.info("Category page %d: %d scanned, %d measured, %d qualifying", page_index + 1, len(games), page_measured, page_qualified)
        if not cursor:
            stop_reason = "category_directory_exhausted"
            break
        # Scan every eligible game on this page, even after one below threshold.
        # Skipped games have unknown viewer totals. This ranked-page heuristic
        # considers measured eligible games only, not an exhaustive global proof.
        # An excluded-only or duplicate-only page cannot establish the boundary.
        if page_new_measured and not page_qualified:
            stop_reason = "measured_eligible_page_below_threshold"
            break
        seen_cursors.add(cursor)
        after = cursor
    else:
        raise IncompleteCollection("Category page limit reached before threshold boundary")

    # Ranking is only discovery. Every active enrollment also gets its own
    # complete census, even when it has disappeared from the top directory.
    missing = [entry for game_id, entry in tracking["games"].items()
               if entry["status"] == "active" and game_id not in retained_by_id]
    restored_metadata = {}
    for offset in range(0, len(missing), 100):
        ids = [entry["game_id"] for entry in missing[offset:offset + 100]]
        metadata, _ = reader.get("games", {"id": ids})
        for game in metadata:
            game_id = str(game.get("id") or "")
            if game_id not in ids or not isinstance(game.get("name"), str) or not game["name"]:
                raise IncompleteCollection("Invalid tracked category metadata")
            restored_metadata[game_id] = game
    missing_games = [{"id": entry["game_id"], "name": entry["game_name"],
                      "igdb_id": entry.get("igdb_id"), "box_art_url": entry.get("box_art_url"),
                      **restored_metadata.get(entry["game_id"], {})} for entry in missing]
    metadata_games = [game for game in missing_games if str(game.get("igdb_id") or "").isdigit()
                      and str(game["igdb_id"]) not in requested_igdb_ids]
    hints_clock = now or datetime.now(timezone.utc)
    if include_release_hints and metadata_games:
        extra_hints, status = release_hints(client, metadata_games, hints_clock, deadline=deadline, monotonic=monotonic)
        hints.update(extra_hints)
        hints_statuses.append(status)
        requested_igdb_ids.update(str(game["igdb_id"]) for game in metadata_games)
    for game in missing_games:
        game_id = str(game["id"])
        hint = hints.get(str(game.get("igdb_id")), {})
        prediction = evaluate_date(hint.get("first_release_date"), hint.get("checked_at"), hints_clock,
                                   source="igdb_first_release_date")
        igdb_predictions[game_id] = prediction
        entry = refresh_tracking(game, prediction, hints_clock)
        if entry["status"] != "active":
            continue
        metrics = category_metrics(reader, game_id, max_stream_pages, retain_channels=include_filtered_audience)
        channels = metrics.pop("_channels", None)
        measured_count += 1
        measured_ids.add(game_id)
        retained_by_id[game_id] = {
            "game_id": game_id, "game_name": game["name"], "igdb_id": game.get("igdb_id") or None,
            "box_art_url": game.get("box_art_url"), "verification": verification_for(game_id, observations, hints_clock),
            **metrics,
        }
        if channels is not None:
            channels_by_game[game_id] = channels

    candidates, excluded = list(candidates_by_id.values()), list(excluded_by_id.values())
    measured_rows = {**retained_by_id, **candidates_by_id}
    hints_status = ("disabled" if not include_release_hints else
                    "collection_deadline_exhausted" if "collection_deadline_exhausted" in hints_statuses else
                    "unavailable_or_partial" if "unavailable_or_partial" in hints_statuses else
                    "ok" if hints_statuses else "no_igdb_ids")
    for row in list(measured_rows.values()) + excluded:
        hint = hints.get(str(row["igdb_id"]))
        if hint:
            row["release_evidence"] = hint
    candidates.sort(key=lambda row: (-row["viewer_count"], row["game_id"]))
    priority = {"recent_release": 0, "upcoming": 1, "unknown": 2, "older_release": 3}
    queue = [r for r in candidates if r["verification"]["status"] == "pending"]
    queue.sort(key=lambda row: (priority[row.get("release_evidence", {}).get("release_band", "unknown")], -row["viewer_count"]))
    finished = now or datetime.now(timezone.utc)
    experiment = attach_experiments(candidates, excluded, hints, release_dates, finished,
                                    igdb_predictions=igdb_predictions)
    retained_only = [row for game_id, row in retained_by_id.items() if game_id not in candidates_by_id]
    attach_experiments(retained_only, [], hints, release_dates, finished, igdb_predictions=igdb_predictions)
    follower_coverage = None
    if include_filtered_audience:
        resolver = FollowerResolver(client, followers_cache_path, max_calls=followers_max_calls,
                                    max_seconds=followers_max_seconds, collection_deadline=deadline,
                                    monotonic=monotonic, utcnow=(lambda: now) if now else None)
        follower_coverage = attach_filtered_audience(list(measured_rows.values()), channels_by_game, resolver)
        finished = now or datetime.now(timezone.utc)
    for row in measured_rows.values():
        if steam_matches_by_id is not None:
            row["steam_matches"] = steam_matches_by_id.get(row["game_id"], [])
        # Preserve the exact pre-census date decision at a 30-day boundary.
        prediction = igdb_predictions.get(row["game_id"], {})
        decision_at = prediction.get("evaluated_at")
        enroll_observation(tracking, row, finished, min_viewers=min_viewers, non_game_ids=NON_GAME_IDS,
                           eligibility_at=parse_timestamp(decision_at) if decision_at else clock,
                           allow_twitch_enrollment=not (prediction.get("status") == "evaluated"
                                                        and prediction.get("predicted_new") is False))
    tracking["updated_at"] = timestamp(finished)
    tracked = [row for row in measured_rows.values()
               if tracking["games"].get(row["game_id"], {}).get("status") == "active"]
    tracked.sort(key=lambda row: (-row["viewer_count"], row["game_id"]))
    return {
        "schema_version": 2, "generated_at": timestamp(finished), "collection_started_at": timestamp(clock),
        "source": "Twitch Helix API", "min_viewers": min_viewers,
        "coverage": {
            "collection_complete": True, "stop_reason": stop_reason,
            "max_collection_seconds": max_collection_seconds,
            "collection_elapsed_seconds": round(monotonic() - collection_started, 3),
            "filtered_audience_complete": (follower_coverage["partial_categories"] == 0 if follower_coverage else None),
            "category_pages": page_index + 1, "categories_seen": len(seen_ids),
            "categories_measured": len(measured_ids), "category_measurements": measured_count,
            "duplicate_category_remeasurements": measured_count - len(measured_ids),
            "below_threshold_measurements": below_count,
            "below_threshold_count": len(below_ids),
            "excluded_before_metrics_count": len(excluded),
            "excluded_by_igdb_date_count": sum(r["reason"] == "igdb_release_outside_window" for r in excluded),
            "igdb_categories_evaluated": sum(p["status"] == "evaluated" for p in igdb_predictions.values()),
            "igdb_categories_unknown": sum(p["status"] == "unknown" for p in igdb_predictions.values()),
            "helix_calls_excluding_retries": reader.calls + (follower_coverage["follower_lookup_calls"] if follower_coverage else 0),
            "census_helix_calls_excluding_retries": reader.calls,
            **({"filtered_audience": follower_coverage} if follower_coverage else {}),
            "global_metrics_are_sampled": False, "is_simultaneous_global_snapshot": False,
            "all_categories_enumerated": stop_reason == "category_directory_exhausted",
            "discovery_note": "Uses Helix category ranking and stops when a page has newly measured eligible categories but none measured reaches the threshold. Excluded categories have unmeasured viewer totals; excluded-only and duplicate-only pages continue. Live rankings and streams can change during pagination; this is not proof of a simultaneous, exhaustive global census.",
            "region_note": "Broadcast language is not broadcaster location; no Taiwan/Asia inference is applied.",
            "igdb_hints_status": hints_status,
            "tracked_active_count": len(tracked),
            "tracked_outside_discovery_count": sum(row["game_id"] not in candidates_by_id for row in tracked),
            "tracking_note": "Twitch new-game discovery and Steam releases within 30 days independently admit categories. Active source memberships are unioned by Twitch ID and measured once; directory absence, viewer decline and badge disappearance do not remove active memberships.",
        },
        "candidate_games": candidates,
        "top_games": [r for r in candidates if r["verification"]["status"] == "new"],
        "pending_verification": [r["game_id"] for r in queue],
        "excluded_games": excluded,
        "newness_experiment": experiment,
        "tracked_games": tracked,
        "tracking_state": tracking,
        "steam_mapping_state": mappings, "steam_catalog_summary": steam_summary,
    }
