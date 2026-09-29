"""Follower-qualified audience statistics, with a bounded, public-total cache.

Raw stream rows and the cache stay on the runner. Only aggregate statistics
are included in the public snapshot. Failures are unknown, never zero followers.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import math
import os
from pathlib import Path
from statistics import median
import tempfile
import time
from typing import Callable

import requests
from urllib3.util import Timeout

from collectors.twitch_live import TWITCH_API_BASE, TwitchClient
from collectors.twitch_newness import parse_timestamp, timestamp

LOGGER = logging.getLogger(__name__)
RULE = "followers_gt_1000_viewers_gte_10_v1"
CACHE_PATH = Path(".cache/twitch_followers.json")
MAX_AGE = timedelta(hours=24)
MIN_FOLLOWERS = 1000
MIN_VIEWERS = 10


def _fresh_record(row: object, now: datetime) -> dict | None:
    if not isinstance(row, dict) or type(row.get("total")) is not int or row["total"] < 0:
        return None
    try:
        observed = parse_timestamp(row.get("observed_at"))
    except (ValueError, TypeError):
        return None
    if not observed <= now < observed + MAX_AGE:
        return None
    return {"total": row["total"], "observed_at": timestamp(observed)}


class FollowerResolver:
    """Resolve only public follower totals; share successes/failures across games."""

    def __init__(self, client: TwitchClient, cache_path: str | Path = CACHE_PATH, *,
                 max_calls: int | None = None, max_seconds: float | None = None,
                 collection_deadline: float | None = None,
                 utcnow: Callable[[], datetime] | None = None,
                 monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        if max_calls is not None and (type(max_calls) is not int or max_calls < 0):
            raise ValueError("Optional follower call budget must be a non-negative integer")
        if max_seconds is not None and (not math.isfinite(max_seconds) or max_seconds <= 0):
            raise ValueError("Optional follower time budget must be positive and finite")
        if collection_deadline is not None and not math.isfinite(collection_deadline):
            raise ValueError("Collection deadline must be finite")
        self.client, self.cache_path = client, Path(cache_path)
        self.max_calls, self.max_seconds = max_calls, max_seconds
        self.collection_deadline = collection_deadline
        self.utcnow = utcnow or (lambda: datetime.now(timezone.utc))
        self.monotonic, self.sleep = monotonic, sleep
        self.started = monotonic()
        self.calls = self.cache_hits = self.failures = self.consecutive_failures = 0
        self.successes = self.checkpoints_saved = 0
        self.stop_reason: str | None = None
        self.results: dict[str, int | None] = {}
        self.cache: dict[str, dict] = {}
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("schema_version") != 1 or not isinstance(payload.get("followers"), dict):
                raise ValueError("Invalid follower cache")
            now = self.utcnow()
            for user_id, row in payload["followers"].items():
                if isinstance(user_id, str) and user_id.isascii() and user_id.isdigit():
                    clean = _fresh_record(row, now)
                    if clean is not None:
                        self.cache[user_id] = clean
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError):
            LOGGER.warning("Follower cache unavailable or invalid; refresh within the lookup budget")

    def remaining(self) -> tuple[float, str]:
        """The original collection deadline is shared; enrichment never resets it."""
        limits = []
        if self.collection_deadline is not None:
            limits.append((self.collection_deadline, "collection_deadline_exhausted"))
        if self.max_seconds is not None:
            limits.append((self.started + self.max_seconds, "time_budget_exhausted"))
        if not limits:
            return math.inf, ""
        deadline, reason = min(limits)
        return deadline - self.monotonic(), reason

    def resolve(self, user_id: str) -> int | None:
        if user_id in self.results and self.results[user_id] is None:
            return None
        if not isinstance(user_id, str) or not user_id.isascii() or not user_id.isdigit():
            return None
        cached = _fresh_record(self.cache.get(user_id), self.utcnow())
        if cached is not None:
            if user_id not in self.results:
                self.cache_hits += 1
            self.results[user_id] = cached["total"]
            return cached["total"]
        self.cache.pop(user_id, None)
        self.results[user_id] = None
        if self.stop_reason:
            return None
        if self.max_calls is not None and self.calls >= self.max_calls:
            self.stop_reason = "call_budget_exhausted"
            return None
        remaining, deadline_reason = self.remaining()
        if remaining <= 0:
            self.stop_reason = deadline_reason
            return None
        # Collection already authenticated this same app. Never request a user
        # token, follower identities, subscriptions, or private web endpoints.
        if not self.client.access_token:
            self.stop_reason = "app_token_unavailable"
            return None
        # One attempt per broadcaster avoids retry/backoff extending enrichment
        # by minutes or preventing the complete base census from being published.
        wait = max(0.0, self.client.request_interval - (self.monotonic() - self.client._last_request_at))
        if wait >= remaining:
            self.stop_reason = deadline_reason
            return None
        if wait:
            self.sleep(wait)
        remaining, deadline_reason = self.remaining()
        if remaining <= 0:
            self.stop_reason = deadline_reason
            return None
        self.calls += 1
        status = None
        try:
            response = self.client.session.get(
                f"{TWITCH_API_BASE}/channels/followers",
                params={"broadcaster_id": user_id},
                headers={"Client-Id": self.client.client_id, "Authorization": f"Bearer {self.client.access_token}"},
                timeout=(Timeout(total=remaining, connect=min(self.client.timeout_seconds, 8.0, remaining),
                                 read=min(self.client.timeout_seconds, 8.0, remaining))
                         if math.isfinite(remaining) else min(self.client.timeout_seconds, 8.0)),
            )
            status = response.status_code
            response.raise_for_status()
            payload = response.json()
            total = payload.get("total") if isinstance(payload, dict) else None
            if type(total) is not int or total < 0:
                raise ValueError("Invalid follower total")
            self.cache[user_id] = {"total": total, "observed_at": timestamp(self.utcnow())}
            self.results[user_id] = total
            self.consecutive_failures = 0
            self.successes += 1
            if self.successes % 50 == 0:
                self.checkpoints_saved += int(self.save())
            return total
        except (requests.RequestException, ValueError, TypeError):
            # Do not print URLs, response bodies, headers, credentials, or exceptions.
            self.failures += 1
            self.consecutive_failures += 1
            if self.remaining()[0] <= 0:
                self.stop_reason = self.remaining()[1]
            elif status == 429:
                self.stop_reason = "rate_limited"
            elif status in {401, 403}:
                self.stop_reason = "authorization_unavailable"
            elif self.consecutive_failures >= 3:
                self.stop_reason = "repeated_lookup_failures"
            return None
        finally:
            self.client._last_request_at = self.monotonic()

    def save(self) -> bool:
        """Atomically persist fresh successes even when the lookup budget ended."""
        now = self.utcnow()
        rows = {user_id: clean for user_id, row in self.cache.items()
                if (clean := _fresh_record(row, now)) is not None}
        temporary = None
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.cache_path.parent,
                                             prefix=".followers-", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                json.dump({"schema_version": 1, "followers": rows}, handle, separators=(",", ":"), sort_keys=True)
                handle.write("\n")
            os.replace(temporary, self.cache_path)
            return True
        except OSError:
            LOGGER.warning("Follower cache could not be saved; base metrics remain publishable")
            return False
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass


def filtered_metrics(channels: list[dict], resolver: FollowerResolver) -> dict:
    eligible, low_viewers, low_followers, unknown = [], 0, 0, 0
    for stream in sorted(channels, key=lambda row: (-row["viewer_count"], str(row["user_id"]))):
        viewers = stream["viewer_count"]
        if viewers < MIN_VIEWERS:
            low_viewers += 1
            continue
        followers = resolver.resolve(str(stream["user_id"]))
        if followers is None:
            unknown += 1
        elif followers <= MIN_FOLLOWERS:
            low_followers += 1
        else:
            eligible.append(viewers)
    return {
        "rule": RULE, "min_followers_exclusive": MIN_FOLLOWERS,
        "min_viewers_inclusive": MIN_VIEWERS, "followers_max_age_hours": 24,
        "status": "partial" if unknown else "complete",
        "median_viewer_count": median(eligible) if eligible and not unknown else None,
        "eligible_streamer_count": len(eligible), "eligible_viewer_count": sum(eligible),
        "excluded_low_viewer_count": low_viewers, "excluded_low_follower_count": low_followers,
        "unknown_follower_count": unknown,
    }


def attach_filtered_audience(candidates: list[dict], channels_by_game: dict[str, list[dict]],
                             resolver: FollowerResolver) -> dict:
    def priority(row):
        experiments = row.get("release_experiment", {})
        signal = any(isinstance(trial, dict) and trial.get("predicted_new") is True for trial in experiments.values())
        rank = 0 if row.get("verification", {}).get("status") == "new" else 1 if signal else 2
        return rank, -row["viewer_count"], row["game_id"]

    try:
        for index, row in enumerate(sorted(candidates, key=priority), 1):
            row["filtered_audience"] = filtered_metrics(channels_by_game[row["game_id"]], resolver)
            metrics = row["filtered_audience"]
            LOGGER.info("Filtered audience %d/%d, game %s: %s, %d qualified channels, %d unknown; %d lookups / %d cache hits",
                        index, len(candidates), row["game_id"], metrics["status"],
                        metrics["eligible_streamer_count"], metrics["unknown_follower_count"],
                        resolver.calls, resolver.cache_hits)
    finally:
        saved = resolver.save()
    complete = sum(row["filtered_audience"]["status"] == "complete" for row in candidates)
    return {
        "rule": RULE, "complete_categories": complete, "partial_categories": len(candidates) - complete,
        "follower_lookup_calls": resolver.calls, "follower_cache_hits": resolver.cache_hits,
        "follower_lookup_failures": resolver.failures,
        "unknown_unique_broadcasters": sum(total is None for total in resolver.results.values()),
        "max_lookup_calls": resolver.max_calls, "max_lookup_seconds": resolver.max_seconds,
        "lookup_elapsed_seconds": round(resolver.monotonic() - resolver.started, 3),
        "cache_checkpoints_saved": resolver.checkpoints_saved,
        "stop_reason": resolver.stop_reason or ("completed_with_unknown_followers" if resolver.failures else "all_required_followers_resolved"),
        "cache_saved": saved,
    }
