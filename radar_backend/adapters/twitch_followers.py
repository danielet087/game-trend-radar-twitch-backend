"""Public follower-total HTTP request; retry policy stays in the application."""

from __future__ import annotations


def request_followers(client, user_id, remaining, *, api_base, timeout_type, isfinite):
    return client.session.get(
        f"{api_base}/channels/followers",
        params={"broadcaster_id": user_id},
        headers={"Client-Id": client.client_id, "Authorization": f"Bearer {client.access_token}"},
        timeout=(
            timeout_type(
                total=remaining,
                connect=min(client.timeout_seconds, 8.0, remaining),
                read=min(client.timeout_seconds, 8.0, remaining),
            )
            if isfinite(remaining)
            else min(client.timeout_seconds, 8.0)
        ),
    )
