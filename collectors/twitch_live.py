from __future__ import annotations

import json
import logging
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests

from radar_backend.adapters.twitch_http import TWITCH_API_BASE, TWITCH_TOKEN_URL, TwitchClient
from radar_backend.domain.twitch import CollectionDeadlineExceeded, TwitchGame
from radar_backend.state.json_snapshot import write_json

LOGGER = logging.getLogger(__name__)

DEFAULT_STEAM_JSON_URL = (
    "https://raw.githubusercontent.com/danielet087/game-trend-radar/"
    "main/data/steam_upcoming.json"
)


def load_steam_games(url: str = DEFAULT_STEAM_JSON_URL, *, timeout_seconds: float = 15.0) -> list[dict[str, Any]]:
    try:
        response = requests.get(url, timeout=timeout_seconds)
        if response.status_code == 404:
            LOGGER.info("Steam public JSON is not available yet; tracked Twitch games will be empty")
            return []
        response.raise_for_status()
        payload = response.json()
        games = payload.get("games") or []
        return [game for game in games if isinstance(game, dict)]
    except (requests.RequestException, ValueError, TypeError) as exc:
        LOGGER.warning("Could not load Steam public JSON: %s", exc)
        return []


def aggregate_streams(streams: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}

    for stream in streams:
        game_id = str(stream.get("game_id") or "")
        if not game_id:
            continue

        bucket = buckets.setdefault(
            game_id,
            {
                "game_id": game_id,
                "game_name": str(stream.get("game_name") or ""),
                "streamer_count": 0,
                "viewer_count": 0,
                "languages": Counter(),
            },
        )
        bucket["streamer_count"] += 1
        bucket["viewer_count"] += int(stream.get("viewer_count") or 0)

        language = str(stream.get("language") or "other")
        bucket["languages"][language] += 1

    result: list[dict[str, Any]] = []
    for bucket in buckets.values():
        languages: Counter = bucket.pop("languages")
        bucket["language_streamers"] = dict(languages.most_common())
        result.append(bucket)

    result.sort(
        key=lambda row: (
            -int(row["viewer_count"]),
            -int(row["streamer_count"]),
            str(row["game_name"]).casefold(),
        )
    )
    return result


def build_tracked_game_summary(
    steam_games: list[dict[str, Any]],
    twitch_games: dict[str, TwitchGame],
    tracked_streams: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    aggregates = {row["game_id"]: row for row in aggregate_streams(tracked_streams)}
    rows: list[dict[str, Any]] = []

    for steam_game in steam_games:
        steam_name = str(steam_game.get("name") or "").strip()
        twitch_game = twitch_games.get(steam_name.casefold())
        if not twitch_game:
            rows.append(
                {
                    "steam_appid": steam_game.get("appid"),
                    "steam_name": steam_name,
                    "twitch_game_id": None,
                    "twitch_name": None,
                    "matched": False,
                    "streamer_count": 0,
                    "viewer_count": 0,
                    "language_streamers": {},
                }
            )
            continue

        aggregate = aggregates.get(twitch_game.id) or {}
        rows.append(
            {
                "steam_appid": steam_game.get("appid"),
                "steam_name": steam_name,
                "twitch_game_id": twitch_game.id,
                "twitch_name": twitch_game.name,
                "matched": True,
                "streamer_count": int(aggregate.get("streamer_count") or 0),
                "viewer_count": int(aggregate.get("viewer_count") or 0),
                "language_streamers": aggregate.get("language_streamers") or {},
                "box_art_url": twitch_game.box_art_url,
                "igdb_id": twitch_game.igdb_id,
            }
        )

    rows.sort(
        key=lambda row: (
            -int(row["viewer_count"]),
            -int(row["streamer_count"]),
            str(row["steam_name"]).casefold(),
        )
    )
    return rows


def collect_twitch(
    *,
    client_id: str,
    client_secret: str,
    global_pages: int = 10,
    tracked_pages: int = 50,
    top_games_limit: int = 100,
    steam_json_url: str = DEFAULT_STEAM_JSON_URL,
) -> dict[str, Any]:
    client = TwitchClient(client_id, client_secret)
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    # Global discovery is intentionally a stable sample of Twitch's most-viewed streams,
    # not a claim that every live channel on Twitch was enumerated.
    global_streams = client.get_stream_pages(max_pages=global_pages)
    global_aggregates = aggregate_streams(global_streams)
    global_game_meta = client.get_games_by_ids(row["game_id"] for row in global_aggregates)

    top_games: list[dict[str, Any]] = []
    for row in global_aggregates:
        meta = global_game_meta.get(row["game_id"])
        if not meta or not meta.igdb_id:
            continue
        enriched = dict(row)
        enriched["box_art_url"] = meta.box_art_url
        enriched["igdb_id"] = meta.igdb_id
        top_games.append(enriched)
        if len(top_games) >= top_games_limit:
            break

    steam_games = load_steam_games(steam_json_url)
    names = [str(game.get("name") or "") for game in steam_games]
    twitch_games = client.get_games_by_names(names) if names else {}

    tracked_ids = [game.id for game in twitch_games.values()]
    tracked_streams: list[dict[str, Any]] = []
    if tracked_ids:
        tracked_streams = client.get_stream_pages(
            game_ids=tracked_ids[:100],
            max_pages=tracked_pages,
        )

    tracked_games = build_tracked_game_summary(
        steam_games,
        twitch_games,
        tracked_streams,
    )

    return {
        "generated_at": generated_at,
        "source": "Twitch Helix API",
        "coverage": {
            "global_stream_sample_size": len(global_streams),
            "global_stream_pages": global_pages,
            "global_metrics_are_sampled": True,
            "global_game_filter": "Twitch categories with a non-empty IGDB game ID",
            "tracked_game_stream_pages": tracked_pages,
            "tracked_game_count": len(steam_games),
            "tracked_game_matches": sum(1 for row in tracked_games if row["matched"]),
            "region_note": (
                "Twitch Helix exposes broadcast language but not broadcaster physical location. "
                "No Taiwan/Asia location inference is applied in this dataset."
            ),
        },
        "top_games": top_games,
        "tracked_games": tracked_games,
    }


def collect_from_environment(
    *,
    output_path: str | Path = "output/twitch_live.json",
    global_pages: int = 10,
    tracked_pages: int = 50,
) -> Path:
    client_id = os.environ.get("TWITCH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("TWITCH_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise RuntimeError("TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET are required")

    payload = collect_twitch(
        client_id=client_id,
        client_secret=client_secret,
        global_pages=global_pages,
        tracked_pages=tracked_pages,
    )
    return write_json(payload, output_path)
