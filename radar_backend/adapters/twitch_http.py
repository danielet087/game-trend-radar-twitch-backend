"""Twitch OAuth/Helix adapter; retry and deadline policy is unchanged."""
from __future__ import annotations

import logging
import time
from typing import Any, Iterable

import requests
from urllib3.util import Timeout

from radar_backend.domain.twitch import CollectionDeadlineExceeded, TwitchGame

LOGGER = logging.getLogger("collectors.twitch_live")
TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_API_BASE = "https://api.twitch.tv/helix"


class TwitchClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        timeout_seconds: float = 20.0,
        request_interval: float = 0.15,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout_seconds = timeout_seconds
        self.request_interval = max(0.0, request_interval)
        self.session = requests.Session()
        self.access_token: str | None = None
        self._last_request_at = 0.0
        self.collection_deadline: float | None = None
        self.monotonic = time.monotonic
        self.sleep = time.sleep

    def _request_timeout(self):
        if self.collection_deadline is None:
            return self.timeout_seconds
        remaining = self.collection_deadline - self.monotonic()
        if remaining <= 0:
            raise CollectionDeadlineExceeded("collection_deadline_exhausted")
        return Timeout(total=remaining, connect=min(self.timeout_seconds, remaining),
                       read=min(self.timeout_seconds, remaining))

    def _sleep_with_deadline(self, delay: float) -> None:
        if self.collection_deadline is not None and self.monotonic() + delay >= self.collection_deadline:
            raise CollectionDeadlineExceeded("collection_deadline_exhausted")
        if delay > 0:
            self.sleep(delay)

    def authenticate(self) -> None:
        # Send credentials in the request body, never in a URL or an exception.
        try:
            response = self.session.post(
                TWITCH_TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "client_credentials",
                },
                timeout=self._request_timeout(),
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            reason = f"HTTP {status}" if status is not None else type(exc).__name__
            raise RuntimeError(f"Twitch OAuth request failed: {reason}") from None
        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise RuntimeError("Twitch OAuth response did not include access_token")
        self.access_token = str(token)

    def _wait(self) -> None:
        if self.request_interval <= 0:
            return
        now = self.monotonic()
        delay = self.request_interval - (now - self._last_request_at)
        if delay > 0:
            self._sleep_with_deadline(delay)

    def get(
        self,
        endpoint: str,
        *,
        params: list[tuple[str, str]] | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.access_token:
            self.authenticate()

        url = f"{TWITCH_API_BASE}/{endpoint.lstrip('/')}"
        headers = {
            "Client-Id": self.client_id,
            "Authorization": f"Bearer {self.access_token}",
        }

        last_error: Exception | str | None = None
        for attempt in range(5):
            self._wait()
            try:
                response = self.session.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=self._request_timeout(),
                )
                self._last_request_at = self.monotonic()

                if response.status_code == 401 and attempt == 0:
                    self.access_token = None
                    self.authenticate()
                    headers["Authorization"] = f"Bearer {self.access_token}"
                    continue

                if response.status_code == 429:
                    reset_at = response.headers.get("Ratelimit-Reset")
                    delay = 10.0
                    if reset_at and reset_at.isdigit():
                        delay = max(1.0, int(reset_at) - time.time() + 1.0)
                    delay = min(delay, 120.0)
                    LOGGER.warning("Twitch rate limit reached; sleeping %.1fs", delay)
                    self._sleep_with_deadline(delay)
                    continue

                if response.status_code in {500, 502, 503, 504}:
                    delay = min(2 ** attempt, 30)
                    last_error = f"HTTP {response.status_code}"
                    self._sleep_with_deadline(delay)
                    continue

                response.raise_for_status()
                return response.json()

            except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                last_error = f"HTTP {status}" if status is not None else type(exc).__name__
                if attempt == 4:
                    break
                self._sleep_with_deadline(min(2 ** attempt, 30))

        raise RuntimeError(f"Twitch API request failed: {last_error}")

    def get_stream_pages(
        self,
        *,
        game_ids: Iterable[str] | None = None,
        max_pages: int = 10,
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        page_size = max(1, min(page_size, 100))
        max_pages = max(1, max_pages)
        game_ids = [str(x) for x in (game_ids or []) if str(x)]

        params: list[tuple[str, str]] = [("first", str(page_size))]
        for game_id in game_ids[:100]:
            params.append(("game_id", game_id))

        streams: list[dict[str, Any]] = []
        after: str | None = None

        for page in range(max_pages):
            page_params = list(params)
            if after:
                page_params.append(("after", after))

            payload = self.get("streams", params=page_params)
            rows = payload.get("data") or []
            if not isinstance(rows, list) or not rows:
                break

            streams.extend(row for row in rows if isinstance(row, dict))
            LOGGER.info(
                "Twitch streams page %d/%d: %d rows, %d total",
                page + 1,
                max_pages,
                len(rows),
                len(streams),
            )

            cursor = ((payload.get("pagination") or {}).get("cursor"))
            if not cursor or len(rows) < page_size:
                break
            after = str(cursor)

        return streams

    def get_games_by_names(self, names: Iterable[str]) -> dict[str, TwitchGame]:
        names = [str(name).strip() for name in names if str(name).strip()]
        found: dict[str, TwitchGame] = {}

        for start in range(0, len(names), 100):
            batch = names[start:start + 100]
            params: list[tuple[str, str]] = [("name", name) for name in batch]
            payload = self.get("games", params=params)

            for row in payload.get("data") or []:
                if not isinstance(row, dict):
                    continue
                game = TwitchGame(
                    id=str(row.get("id") or ""),
                    name=str(row.get("name") or ""),
                    box_art_url=str(row.get("box_art_url") or "") or None,
                    igdb_id=str(row.get("igdb_id") or "") or None,
                )
                if game.id and game.name:
                    found[game.name.casefold()] = game

        return found

    def get_games_by_ids(self, game_ids: Iterable[str]) -> dict[str, TwitchGame]:
        game_ids = list(dict.fromkeys(str(game_id).strip() for game_id in game_ids if str(game_id).strip()))
        found: dict[str, TwitchGame] = {}

        for start in range(0, len(game_ids), 100):
            batch = game_ids[start:start + 100]
            params: list[tuple[str, str]] = [("id", game_id) for game_id in batch]
            payload = self.get("games", params=params)

            for row in payload.get("data") or []:
                if not isinstance(row, dict):
                    continue
                game = TwitchGame(
                    id=str(row.get("id") or ""),
                    name=str(row.get("name") or ""),
                    box_art_url=str(row.get("box_art_url") or "") or None,
                    igdb_id=str(row.get("igdb_id") or "") or None,
                )
                if game.id:
                    found[game.id] = game

        return found

