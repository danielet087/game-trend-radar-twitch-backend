"""Threshold discovery and stream census, separate from NEW-badge verification.

Helix does not expose Twitch's NEW badge. Only dated, explicit observations
may confirm it; the 14-day date experiment never overrides that classification.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
from statistics import median
import time
from typing import Any

import requests

from collectors.twitch_live import TwitchClient
from collectors.twitch_newness import (
    RELEASE_DATES_PATH, attach_experiments, load_release_dates, parse_timestamp, timestamp,
)

LOGGER = logging.getLogger(__name__)
REGISTRY_PATH = Path(__file__).resolve().parents[1] / "data/twitch_category_verification.json"
IGDB_URL = "https://api.igdb.com/v4/games"
NON_GAME_IDS = {"509658": "Just Chatting", "509672": "IRL", "509663": "Special Events"}


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
    def __init__(self, client: TwitchClient, max_calls: int):
        self.client, self.max_calls, self.calls = client, max_calls, 0

    def get(self, endpoint: str, params: dict) -> tuple[list[dict], str | None]:
        if self.calls >= self.max_calls:
            raise IncompleteCollection("Helix call budget exhausted; nothing will be published")
        self.calls += 1
        payload = self.client.get(endpoint, params=params)
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


def category_metrics(reader: PageReader, game_id: str, max_pages: int = 150) -> dict:
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
    return {
        "viewer_count": sum(counts), "streamer_count": len(counts),
        "median_viewer_count": median(counts) if counts else None,
        "language_streamers": dict(languages.most_common()),
        "measurement_started_at": started, "measurement_finished_at": timestamp(datetime.now(timezone.utc)),
        "pagination_complete": True, "stream_pages": page_index + 1,
        "duplicate_broadcasters_removed": duplicates,
    }


def release_hints(client: TwitchClient, games: list[dict], now: datetime) -> tuple[dict, str]:
    ids = sorted({str(row.get("igdb_id")) for row in games if str(row.get("igdb_id") or "").isdigit()})
    if not ids:
        return {}, "no_igdb_ids"
    hints = {}
    # One batch is at most 100 IDs. These dates are supporting evidence only.
    for offset in range(0, len(ids), 100):
        query = "fields id,name,first_release_date; where id = (" + ",".join(ids[offset:offset + 100]) + "); limit 100;"
        try:
            client._wait()
            try:
                response = client.session.post(
                    IGDB_URL, data=query,
                    headers={"Client-ID": client.client_id, "Authorization": f"Bearer {client.access_token}"},
                    timeout=client.timeout_seconds,
                )
            finally:
                client._last_request_at = time.monotonic()
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list):
                raise ValueError("Invalid IGDB response")
            for row in rows:
                if not isinstance(row, dict):
                    continue
                raw_date = row.get("first_release_date")
                date = datetime.fromtimestamp(raw_date, timezone.utc) if type(raw_date) in (int, float) else None
                age = (now - date).days if date else None
                band = "unknown" if age is None else "upcoming" if age < 0 else "recent_release" if age <= 30 else "older_release"
                hints[str(row["id"])] = {
                    "source": "IGDB", "checked_at": timestamp(now),
                    "first_release_date": timestamp(date) if date else None,
                    "release_band": band, "confirms_twitch_new_badge": False,
                }
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
) -> dict[str, Any]:
    if min(min_viewers, max_category_pages, max_stream_pages, max_api_calls) < 1 or not 1 <= category_page_size <= 100:
        raise ValueError("Threshold and limits must be positive; page size must be 1..100")
    clock = now or datetime.now(timezone.utc)
    observations = load_verifications(registry_path)
    release_dates = load_release_dates(release_dates_path)
    client = client or TwitchClient(client_id, client_secret, request_interval=0.3)
    reader = PageReader(client, max_api_calls)
    candidates_by_id, excluded_by_id = {}, {}
    seen_ids, seen_cursors = set(), set()
    measured_ids = set()
    below_ids = set()
    after = None
    measured_count = below_count = 0
    stop_reason = None
    for page_index in range(max_category_pages):
        params: dict[str, Any] = {"first": category_page_size}
        if after:
            params["after"] = after
        games, cursor = reader.get("games/top", params)
        if cursor and cursor in seen_cursors:
            raise IncompleteCollection("Repeated category cursor")
        page_measured, page_qualified, page_new_measured = 0, 0, 0
        page_seen = set()
        for game in games:
            game_id = str(game.get("id") or "")
            if not game_id.isdigit() or not isinstance(game.get("name"), str) or not game["name"]:
                raise IncompleteCollection("Invalid category metadata")
            if game_id in page_seen:
                continue
            page_seen.add(game_id)
            previously_seen = game_id in seen_ids
            seen_ids.add(game_id)
            verification = verification_for(game_id, observations, now or datetime.now(timezone.utc))
            if game_id in NON_GAME_IDS or verification["status"] == "not_new":
                candidates_by_id.pop(game_id, None)
                below_ids.discard(game_id)
                excluded_by_id[game_id] = {
                    "game_id": game_id, "game_name": game["name"],
                    "igdb_id": game.get("igdb_id") or None,
                    "reason": "non_game_category" if game_id in NON_GAME_IDS else "observed_not_new",
                    "verification": verification, "metrics_collected": False,
                }
                continue
            excluded_by_id.pop(game_id, None)
            # Re-measure cross-page duplicates. Merely seeing one duplicate must
            # not disable threshold termination on every subsequent page.
            metrics = category_metrics(reader, game_id, max_stream_pages)
            measured_count += 1
            measured_ids.add(game_id)
            page_measured += 1
            page_new_measured += int(not previously_seen)
            if metrics["viewer_count"] < min_viewers:
                below_count += 1
                below_ids.add(game_id)
                candidates_by_id.pop(game_id, None)
                continue
            page_qualified += 1
            below_ids.discard(game_id)
            candidates_by_id[game_id] = {
                "game_id": game_id, "game_name": game["name"],
                "box_art_url": game.get("box_art_url"), "igdb_id": game.get("igdb_id") or None,
                "verification": verification, **metrics,
            }
        LOGGER.info("Category page %d: %d scanned, %d measured, %d qualifying", page_index + 1, len(games), page_measured, page_qualified)
        if not cursor:
            stop_reason = "category_directory_exhausted"
            break
        # Scan the entire page, not merely the first under-threshold category.
        # A duplicate-only or excluded-only page still cannot establish a boundary.
        if page_new_measured and not page_qualified:
            stop_reason = "whole_page_below_threshold"
            break
        seen_cursors.add(cursor)
        after = cursor
    else:
        raise IncompleteCollection("Category page limit reached before threshold boundary")

    candidates, excluded = list(candidates_by_id.values()), list(excluded_by_id.values())
    metadata_games = candidates + [r for r in excluded if r["reason"] == "observed_not_new"]
    hints_clock = now or datetime.now(timezone.utc)
    hints, hints_status = release_hints(client, metadata_games, hints_clock) if include_release_hints else ({}, "disabled")
    for row in candidates:
        hint = hints.get(str(row["igdb_id"]))
        if hint:
            row["release_evidence"] = hint
    candidates.sort(key=lambda row: (-row["viewer_count"], row["game_id"]))
    priority = {"recent_release": 0, "upcoming": 1, "unknown": 2, "older_release": 3}
    queue = [r for r in candidates if r["verification"]["status"] == "pending"]
    queue.sort(key=lambda row: (priority[row.get("release_evidence", {}).get("release_band", "unknown")], -row["viewer_count"]))
    finished = now or datetime.now(timezone.utc)
    experiment = attach_experiments(candidates, excluded, hints, release_dates, finished)
    return {
        "schema_version": 2, "generated_at": timestamp(finished), "collection_started_at": timestamp(clock),
        "source": "Twitch Helix API", "min_viewers": min_viewers,
        "coverage": {
            "collection_complete": True, "stop_reason": stop_reason,
            "category_pages": page_index + 1, "categories_seen": len(seen_ids),
            "categories_measured": len(measured_ids), "category_measurements": measured_count,
            "duplicate_category_remeasurements": measured_count - len(measured_ids),
            "below_threshold_measurements": below_count,
            "below_threshold_count": len(below_ids),
            "excluded_before_metrics_count": len(excluded), "helix_calls_excluding_retries": reader.calls,
            "global_metrics_are_sampled": False, "is_simultaneous_global_snapshot": False,
            "all_categories_enumerated": stop_reason == "category_directory_exhausted",
            "discovery_note": "Uses Helix category ranking and stops after a whole measured page below threshold. Live rankings and streams can change during pagination; this is not proof of a simultaneous, exhaustive global census.",
            "region_note": "Broadcast language is not broadcaster location; no Taiwan/Asia inference is applied.",
            "igdb_hints_status": hints_status,
        },
        "candidate_games": candidates,
        "top_games": [r for r in candidates if r["verification"]["status"] == "new"],
        "pending_verification": [r["game_id"] for r in queue],
        "excluded_games": excluded,
        "newness_experiment": experiment,
        "tracked_games": [],
    }
