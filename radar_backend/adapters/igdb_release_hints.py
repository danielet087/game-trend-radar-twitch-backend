"""IGDB metadata request adapter with deadline handling and sanitized errors."""

from __future__ import annotations
from datetime import datetime
from typing import Callable, Any
import time


def release_hints(
    client: Any,
    games: list[dict],
    now: datetime,
    *,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    igdb_url,
    datetime_type,
    timezone_type,
    timedelta_type,
    timeout_type,
    source_rules,
    timestamp,
    logger,
    request_error,
    deadline_error,
    apply_rows,
) -> tuple[dict, str]:
    ids = sorted(
        {str(row.get("igdb_id")) for row in games if str(row.get("igdb_id") or "").isdigit()}
    )
    if not ids:
        return ({}, "no_igdb_ids")
    hints = {}
    for offset in range(0, len(ids), 100):
        query = (
            "fields id,name,first_release_date; where id = ("
            + ",".join(ids[offset : offset + 100])
            + "); limit 100;"
        )
        try:
            if deadline is not None and monotonic() >= deadline:
                raise deadline_error("collection_deadline_exhausted")
            client._wait()
            remaining = deadline - monotonic() if deadline is not None else None
            if remaining is not None and remaining <= 0:
                raise deadline_error("collection_deadline_exhausted")
            try:
                response = client.session.post(
                    igdb_url,
                    data=query,
                    headers={
                        "Client-ID": client.client_id,
                        "Authorization": f"Bearer {client.access_token}",
                    },
                    timeout=(
                        timeout_type(
                            total=remaining,
                            connect=min(client.timeout_seconds, remaining),
                            read=min(client.timeout_seconds, remaining),
                        )
                        if remaining is not None
                        else client.timeout_seconds
                    ),
                )
            finally:
                client._last_request_at = monotonic()
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list):
                raise ValueError("Invalid IGDB response")
            apply_rows(
                rows,
                now,
                hints,
                datetime_type=datetime_type,
                timezone_type=timezone_type,
                timedelta_type=timedelta_type,
                source_rules=source_rules,
                timestamp=timestamp,
            )
        except deadline_error:
            logger.warning(
                "IGDB hints stopped at collection deadline; remaining metadata stays unknown"
            )
            return (hints, "collection_deadline_exhausted")
        except (request_error, ValueError, KeyError, OverflowError, OSError) as exc:
            # Never log response bodies or headers, or turn failures into exclusions.
            status = getattr(getattr(exc, "response", None), "status_code", None)
            logger.warning(
                "IGDB release hints unavailable (%s); verification stays pending",
                status or type(exc).__name__,
            )
            return (hints, "unavailable_or_partial")
    return (hints, "ok")
