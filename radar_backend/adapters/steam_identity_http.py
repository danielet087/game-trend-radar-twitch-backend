"""Keyless official Steam appdetails transport with the shared collection deadline."""

from __future__ import annotations

from typing import Callable


def get_steam_identity_metadata(
    appid: str,
    checked_at: str,
    *,
    deadline: float | None,
    monotonic: Callable[[], float],
    check_deadline,
    timeout_type,
    request_get,
    appdetails_url,
    payload_metadata,
) -> dict:
    check_deadline(deadline, monotonic)
    remaining = deadline - monotonic() if deadline is not None else 20.0
    timeout = timeout_type(
        total=min(20.0, remaining),
        connect=min(10.0, remaining),
        read=min(20.0, remaining),
    )
    # Only the fixed endpoint is requested; credentials and redirects are never used.
    response = request_get(
        appdetails_url(),
        params={"appids": appid, "cc": "tw", "l": "tchinese"},
        timeout=timeout,
        allow_redirects=False,
    )
    check_deadline(deadline, monotonic)
    response.raise_for_status()
    if response.status_code != 200:
        raise ValueError(
            "Steam identity metadata requires a complete HTTP 200 response"
        )
    payload = response.json()
    check_deadline(deadline, monotonic)
    return payload_metadata(payload, appid, checked_at)
