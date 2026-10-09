"""IGDB identity HTTP and complete pagination with explicit deadline ports."""

from __future__ import annotations
from typing import Callable


def _deadline(
    deadline: float | None,
    monotonic: Callable[[], float],
    *,
    CollectionDeadlineExceeded,
) -> None:
    if deadline is not None and monotonic() >= deadline:
        raise CollectionDeadlineExceeded("collection_deadline_exhausted")


def _igdb(
    client,
    endpoint: str,
    query: str,
    deadline,
    monotonic,
    *,
    _deadline,
    Timeout,
    IGDB_BASE,
) -> list[dict]:
    _deadline(deadline, monotonic)
    if not client.access_token:
        client.authenticate()
    client._wait()
    # The shared collector normally uses 0.3 seconds, but this helper must
    # also respect IGDB's four-per-second limit with a default Twitch client.
    if getattr(client, "request_interval", 0.25) < 0.25:
        delay = 0.25 - (monotonic() - client._last_request_at)
        if delay > 0:
            client._sleep_with_deadline(delay)
    _deadline(deadline, monotonic)
    remaining = deadline - monotonic() if deadline is not None else None
    timeout = (
        Timeout(
            total=remaining,
            connect=min(client.timeout_seconds, remaining),
            read=min(client.timeout_seconds, remaining),
        )
        if remaining is not None
        else client.timeout_seconds
    )
    try:
        response = client.session.post(
            IGDB_BASE + endpoint,
            data=query,
            headers={
                "Client-ID": client.client_id,
                "Authorization": f"Bearer {client.access_token}",
            },
            timeout=timeout,
        )
    finally:
        client._last_request_at = monotonic()
    _deadline(deadline, monotonic)
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list) or not all((isinstance(row, dict) for row in rows)):
        raise ValueError("Invalid IGDB mapping response")
    return rows


def _pages(
    client, endpoint: str, query: str, deadline, monotonic, *, _igdb
) -> list[dict]:
    result = []
    # Stable ID ordering avoids treating the default ten-row response as complete.
    for offset in range(0, 50000, 500):
        rows = _igdb(
            client,
            endpoint,
            f"{query} sort id asc; limit 500; offset {offset};",
            deadline,
            monotonic,
        )
        if len(rows) > 500:
            raise ValueError("IGDB mapping page exceeded limit")
        result.extend(rows)
        if len(rows) < 500:
            return result
    raise ValueError("IGDB mapping pagination exceeded safe limit")


def _error(exc: Exception) -> str:
    # Never publish exception text: requests can contain credentials or bodies.
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return f"HTTP {status}" if type(status) is int else type(exc).__name__
