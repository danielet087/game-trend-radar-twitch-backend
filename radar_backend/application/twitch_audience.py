"""Follower lookup orchestration with explicit transport, time and cache ports."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading

from radar_backend.domain.twitch_audience import chinese_channels

MAX_CONCURRENCY = 8
# Parallel lookups space request starts globally: at most 6.25 requests per
# second (375 per minute), under half of Twitch's 800-point app bucket.
PARALLEL_START_SPACING = 0.16


def lookup_order(channels: list[dict], *, min_viewers: int) -> list[str]:
    """Broadcasters filtered_metrics() resolves, in the same order."""
    return [
        str(stream["user_id"])
        for stream in sorted(channels, key=lambda row: (-row["viewer_count"], str(row["user_id"])))
        if stream["viewer_count"] >= min_viewers
    ]


class FollowerResolver:
    """Share successful and unknown totals while honoring the original budgets."""

    def __init__(
        self,
        client,
        cache_path,
        *,
        max_calls: int | None = None,
        max_seconds: float | None = None,
        collection_deadline: float | None = None,
        concurrency: int = 1,
        worker_client=None,
        parallel_start_spacing: float = PARALLEL_START_SPACING,
        utcnow,
        monotonic,
        sleep,
        path_type,
        load_cache,
        save_cache,
        fresh_record,
        timestamp,
        request,
        request_exception,
        warning,
        math_module,
    ):
        if max_calls is not None and (type(max_calls) is not int or max_calls < 0):
            raise ValueError("Optional follower call budget must be a non-negative integer")
        if max_seconds is not None and (
            not math_module().isfinite(max_seconds) or max_seconds <= 0
        ):
            raise ValueError("Optional follower time budget must be positive and finite")
        if collection_deadline is not None and (not math_module().isfinite(collection_deadline)):
            raise ValueError("Collection deadline must be finite")
        if type(concurrency) is not int or not 1 <= concurrency <= MAX_CONCURRENCY:
            raise ValueError(f"Follower lookup concurrency must be an integer from 1 to {MAX_CONCURRENCY}")
        if not math_module().isfinite(parallel_start_spacing) or parallel_start_spacing < 0:
            raise ValueError("Parallel start spacing must be finite and non-negative")
        self.parallel_start_spacing = parallel_start_spacing
        self.client, self.cache_path = (client, path_type(cache_path))
        self.max_calls, self.max_seconds = (max_calls, max_seconds)
        self.collection_deadline = collection_deadline
        self.concurrency = concurrency
        # A worker client gets its own HTTP session; the default shares the client.
        self.worker_client = worker_client or (lambda shared: shared)
        self._lock = threading.Lock()
        self._local = threading.local()
        self._next_start = float("-inf")
        self.utcnow = utcnow
        self.monotonic, self.sleep = (monotonic, sleep)
        self.load_cache, self.save_cache = load_cache, save_cache
        self.fresh_record, self.timestamp = fresh_record, timestamp
        self.request, self.request_exception = request, request_exception
        self.warning, self.math_module = warning, math_module
        self.started = monotonic()
        self.calls = self.cache_hits = self.failures = self.consecutive_failures = 0
        self.successes = self.checkpoints_saved = 0
        self.stop_reason: str | None = None
        self.results: dict[str, int | None] = {}
        self.cache: dict[str, dict] = {}
        try:
            payload = self.load_cache(self.cache_path)
            now = self.utcnow()
            for user_id, row in payload["followers"].items():
                if isinstance(user_id, str) and user_id.isascii() and user_id.isdigit():
                    clean = self.fresh_record(row, now)
                    if clean is not None:
                        self.cache[user_id] = clean
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError):
            self.warning("Follower cache unavailable or invalid; refresh within the lookup budget")

    def remaining(self) -> tuple[float, str]:
        """The original collection deadline is shared; enrichment never resets it."""
        limits = []
        if self.collection_deadline is not None:
            limits.append((self.collection_deadline, "collection_deadline_exhausted"))
        if self.max_seconds is not None:
            limits.append((self.started + self.max_seconds, "time_budget_exhausted"))
        if not limits:
            return (self.math_module().inf, "")
        deadline, reason = min(limits)
        return (deadline - self.monotonic(), reason)

    def resolve(self, user_id: str) -> int | None:
        if user_id in self.results and self.results[user_id] is None:
            return None
        if not isinstance(user_id, str) or not user_id.isascii() or (not user_id.isdigit()):
            return None
        cached = self.fresh_record(self.cache.get(user_id), self.utcnow())
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
        if not self.client.access_token:
            self.stop_reason = "app_token_unavailable"
            return None
        wait = max(
            0.0, self.client.request_interval - (self.monotonic() - self.client._last_request_at)
        )
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
            response = self.request(self.client, user_id, remaining)
            status = response.status_code
            response.raise_for_status()
            payload = response.json()
            total = payload.get("total") if isinstance(payload, dict) else None
            if type(total) is not int or total < 0:
                raise ValueError("Invalid follower total")
            self.cache[user_id] = {"total": total, "observed_at": self.timestamp(self.utcnow())}
            self.results[user_id] = total
            self.consecutive_failures = 0
            self.successes += 1
            if self.successes % 50 == 0:
                self.checkpoints_saved += int(self.save())
            return total
        except (self.request_exception(), ValueError, TypeError):
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

    def needs_lookup(self, user_id) -> bool:
        """Whether resolve() would have to ask Twitch for this broadcaster now."""
        if not isinstance(user_id, str) or not user_id.isascii() or not user_id.isdigit():
            return False
        if user_id in self.results:
            return False
        return self.fresh_record(self.cache.get(user_id), self.utcnow()) is None

    def prefetch(self, user_ids) -> int:
        """Look up uncached broadcasters in order with bounded parallel requests.

        A sequential resolver does nothing here and resolve() works as before.
        Results land in the shared cache and results, so the later resolve()
        calls return them without another request. Budgets, the shared
        collection deadline and the stop reasons hold across all workers.
        """
        if self.concurrency <= 1 or self.stop_reason:
            return 0
        pending = [user_id for user_id in dict.fromkeys(user_ids) if self.needs_lookup(user_id)]
        if not pending:
            return 0
        clients = []

        def start_worker():
            client = self.worker_client(self.client)
            with self._lock:
                clients.append(client)
            self._local.client = client

        try:
            with ThreadPoolExecutor(
                max_workers=min(self.concurrency, len(pending)),
                thread_name_prefix="followers",
                initializer=start_worker,
            ) as pool:
                for _ in pool.map(self._fetch_in_worker, pending):
                    pass
        finally:
            for client in clients:
                close = getattr(client, "close", None)
                if client is not self.client and callable(close):
                    close()
        return len(pending)

    def _may_request(self, user_id: str) -> bool:
        """Budget checks shared by all workers; call with the lock held."""
        if self.stop_reason or user_id in self.results:
            return False
        if self.max_calls is not None and self.calls >= self.max_calls:
            self.stop_reason = "call_budget_exhausted"
            return False
        remaining, deadline_reason = self.remaining()
        if remaining <= 0:
            self.stop_reason = deadline_reason
            return False
        if not self.client.access_token:
            self.stop_reason = "app_token_unavailable"
            return False
        return True

    def _fetch_in_worker(self, user_id: str) -> None:
        with self._lock:
            if not self._may_request(user_id):
                return
            remaining, deadline_reason = self.remaining()
            now = self.monotonic()
            start = max(now, self._next_start)
            self._next_start = start + self.parallel_start_spacing
            wait = start - now
            if wait >= remaining:
                self.stop_reason = deadline_reason
                return
        if wait > 0:
            self.sleep(wait)
        with self._lock:
            if not self._may_request(user_id):
                return
            remaining = self.remaining()[0]
            self.cache.pop(user_id, None)
            self.results[user_id] = None
            self.calls += 1
        status = None
        try:
            response = self.request(getattr(self._local, "client", self.client), user_id, remaining)
            status = response.status_code
            response.raise_for_status()
            payload = response.json()
            total = payload.get("total") if isinstance(payload, dict) else None
            if type(total) is not int or total < 0:
                raise ValueError("Invalid follower total")
        except (self.request_exception(), ValueError, TypeError):
            with self._lock:
                self.failures += 1
                self.consecutive_failures += 1
                if not self.stop_reason:
                    remaining, deadline_reason = self.remaining()
                    if remaining <= 0:
                        self.stop_reason = deadline_reason
                    elif status == 429:
                        self.stop_reason = "rate_limited"
                    elif status in {401, 403}:
                        self.stop_reason = "authorization_unavailable"
                    elif self.consecutive_failures >= 3:
                        self.stop_reason = "repeated_lookup_failures"
            return
        with self._lock:
            self.cache[user_id] = {"total": total, "observed_at": self.timestamp(self.utcnow())}
            self.results[user_id] = total
            self.consecutive_failures = 0
            self.successes += 1
            if self.successes % 50 == 0:
                self.checkpoints_saved += int(self.save())

    def save(self) -> bool:
        """Persist fresh successes through the atomic cache port."""
        now = self.utcnow()
        rows = {
            user_id: clean
            for user_id, row in self.cache.items()
            if (clean := self.fresh_record(row, now)) is not None
        }
        return self.save_cache(self.cache_path, rows)


def filtered_metrics(
    channels: list[dict], resolver, *, min_viewers, min_followers, summarize_metrics
) -> dict:
    eligible, low_viewers, low_followers, unknown = ([], 0, 0, 0)
    for stream in sorted(channels, key=lambda row: (-row["viewer_count"], str(row["user_id"]))):
        viewers = stream["viewer_count"]
        if viewers < min_viewers():
            low_viewers += 1
            continue
        followers = resolver.resolve(str(stream["user_id"]))
        if followers is None:
            unknown += 1
        elif followers <= min_followers():
            low_followers += 1
        else:
            eligible.append(viewers)
    return summarize_metrics(eligible, low_viewers, low_followers, unknown)


def attach_filtered_audience(
    candidates: list[dict],
    channels_by_game: dict[str, list[dict]],
    resolver,
    *,
    regional_channels_by_game: dict[str, list[dict]] | None = None,
    priority,
    filtered_metrics,
    info,
    rule,
    lookup_order=None,
) -> dict:
    class SharedFollowers:
        """Resolve a broadcaster once for both populations in this census."""
        def __init__(self):
            self.totals = {}

        def resolve(self, user_id):
            if user_id not in self.totals:
                self.totals[user_id] = resolver.resolve(user_id)
            return self.totals[user_id]

    ordered = sorted(candidates, key=priority)
    try:
        if lookup_order is not None and getattr(resolver, "concurrency", 1) > 1:
            # Same order as the sequential pass: categories by priority, then viewers.
            user_ids = []
            for row in ordered:
                user_ids.extend(lookup_order(channels_by_game[row["game_id"]]))
                if regional_channels_by_game is not None and row["game_id"] in regional_channels_by_game:
                    user_ids.extend(lookup_order(regional_channels_by_game[row["game_id"]]))
            started = resolver.monotonic()
            queued = resolver.prefetch(user_ids)
            info(
                "Follower prefetch: %d broadcasters queued, %d parallel requests; %d lookups in %.1fs%s",
                queued,
                resolver.concurrency,
                resolver.calls,
                resolver.monotonic() - started,
                f"; stopped: {resolver.stop_reason}" if resolver.stop_reason else "",
            )
        for index, row in enumerate(ordered, 1):
            shared = SharedFollowers()
            game_id = row["game_id"]
            global_channels = channels_by_game[game_id]
            row["filtered_audience"] = filtered_metrics(global_channels, shared)
            if regional_channels_by_game is None:
                channels = chinese_channels(global_channels)
                regional = row.setdefault("audience_regions", {}).setdefault("zh", {})
                regional.update(viewer_count=sum(channel["viewer_count"] for channel in channels),
                                streamer_count=len(channels),
                                filtered_audience=filtered_metrics(channels, shared))
            elif game_id in regional_channels_by_game:
                row["audience_regions"]["zh"]["filtered_audience"] = filtered_metrics(
                    regional_channels_by_game[game_id], shared
                )
            metrics = row["filtered_audience"]
            info(
                "Filtered audience %d/%d, game %s: %s, %d qualified channels, %d unknown; %d lookups / %d cache hits",
                index,
                len(candidates),
                row["game_id"],
                metrics["status"],
                metrics["eligible_streamer_count"],
                metrics["unknown_follower_count"],
                resolver.calls,
                resolver.cache_hits,
            )
    finally:
        saved = resolver.save()
    complete = sum((row["filtered_audience"]["status"] == "complete" for row in candidates))
    return {
        "rule": rule(),
        "complete_categories": complete,
        "partial_categories": len(candidates) - complete,
        "follower_lookup_calls": resolver.calls,
        "follower_cache_hits": resolver.cache_hits,
        "follower_lookup_failures": resolver.failures,
        "unknown_unique_broadcasters": sum(total is None for total in resolver.results.values()),
        "max_lookup_calls": resolver.max_calls,
        "max_lookup_seconds": resolver.max_seconds,
        "lookup_concurrency": getattr(resolver, "concurrency", 1),
        "lookup_elapsed_seconds": round(resolver.monotonic() - resolver.started, 3),
        "cache_checkpoints_saved": resolver.checkpoints_saved,
        "stop_reason": resolver.stop_reason
        or (
            "completed_with_unknown_followers"
            if resolver.failures
            else "all_required_followers_resolved"
        ),
        "cache_saved": saved,
    }

