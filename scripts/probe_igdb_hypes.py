"""Read one game's official IGDB hypes without collecting or publishing metrics."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re

import requests

from collectors.twitch_live import TwitchClient
from collectors.twitch_newness import timestamp

ENDPOINT = "https://api.igdb.com/v4/games"
DEFAULT_GAME_ID = "366896"
FIELDS = ("id", "name", "hypes", "platforms.name", "release_dates.platform.name", "release_dates.human", "url")
ID = re.compile(r"[1-9][0-9]*\Z", re.ASCII)


def game_id(value: str) -> str:
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise argparse.ArgumentTypeError("game ID must be a canonical positive integer")
    return value


def _text(value, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise ValueError(f"Invalid {label}")
    return value


def _reference(row: dict) -> dict:
    if not isinstance(row, dict):
        raise ValueError("Invalid IGDB platform")
    result = {"name": _text(row.get("name"), "platform name")}
    if "id" in row:
        if type(row["id"]) is not int or row["id"] <= 0:
            raise ValueError("Invalid IGDB platform ID")
        result["id"] = row["id"]
    return result


def validate_result(rows, requested_id: str, checked_at: str) -> dict:
    """Copy only requested fields; absent hypes is unknown, including explicit null."""
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise ValueError("IGDB must return exactly one game")
    row = rows[0]
    if type(row.get("id")) is not int or str(row["id"]) != requested_id:
        raise ValueError("IGDB returned a different game")
    name = _text(row.get("name"), "game name")
    hypes = row.get("hypes")
    if hypes is not None and (type(hypes) is not int or hypes < 0):
        raise ValueError("Invalid IGDB hypes")
    platforms = row.get("platforms", [])
    dates = row.get("release_dates", [])
    if not isinstance(platforms, list) or not isinstance(dates, list):
        raise ValueError("Invalid IGDB platform or release date list")
    platforms = [_reference(platform) for platform in platforms]
    releases = []
    for released in dates:
        if not isinstance(released, dict):
            raise ValueError("Invalid IGDB release date")
        result = {"platform": _reference(released.get("platform")),
                  "human": _text(released.get("human"), "release date")}
        if "id" in released:
            if type(released["id"]) is not int or released["id"] <= 0:
                raise ValueError("Invalid IGDB release date ID")
            result["id"] = released["id"]
        releases.append(result)
    url = row.get("url")
    if url is not None:
        _text(url, "game URL")
    raw = {"id": row["id"], "name": name}
    for field, value in (("hypes", hypes), ("platforms", platforms), ("release_dates", releases), ("url", url)):
        if field in row:
            raw[field] = deepcopy(value)
    return {"schema_version": 1, "status": "ok", "checked_at": checked_at,
            "game_id": requested_id, "name": name, "hypes": hypes,
            "hypes_status": "available" if hypes is not None else "missing",
            "platforms": platforms, "release_dates": releases, "url": url,
            "source": {"provider": "IGDB", "endpoint": ENDPOINT,
                       "requested_fields": list(FIELDS), "raw_fields": raw}}


def probe(client: TwitchClient, requested_id: str = DEFAULT_GAME_ID) -> dict:
    """Authenticate and issue one exact-ID IGDB request, with no retries."""
    requested_id = game_id(requested_id)
    client.authenticate()
    client._wait()
    query = f"fields {','.join(FIELDS)}; where id = {requested_id}; limit 2;"
    try:
        response = client.session.post(ENDPOINT, data=query,
            headers={"Client-ID": client.client_id, "Authorization": f"Bearer {client.access_token}"},
            timeout=client.timeout_seconds, allow_redirects=False)
    finally:
        client._last_request_at = client.monotonic()
    response.raise_for_status()
    if response.status_code != 200:
        raise ValueError("IGDB probe requires a complete HTTP 200 response")
    rows = response.json()
    return validate_result(rows, requested_id, timestamp(datetime.now(timezone.utc)))


def _safe_error(exc: Exception, stage: str) -> dict:
    """Never format an exception, response body, header, URL, or credential."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if type(status) is int:
        reason = "http_error"
    elif isinstance(exc, requests.Timeout):
        reason = "timeout"
    elif isinstance(exc, requests.ConnectionError):
        reason = "connection_error"
    elif isinstance(exc, requests.RequestException):
        reason = "request_error"
    elif isinstance(exc, (ValueError, TypeError, KeyError, AttributeError)):
        reason = "invalid_response"
    elif isinstance(exc, OSError):
        reason = "output_error"
    else:
        reason = "lookup_error"
    result = {"status": "error", "stage": stage, "reason": reason}
    if type(status) is int:
        result["http_status"] = status
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-id", type=game_id, default=DEFAULT_GAME_ID)
    parser.add_argument("--output", type=Path, default=Path("output/igdb_hypes_probe.json"))
    args = parser.parse_args(argv)
    client_id = os.environ.get("TWITCH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("TWITCH_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        print(json.dumps({"status": "error", "stage": "configuration", "reason": "missing_twitch_credentials"}))
        return 1
    stage = "lookup"
    try:
        client = TwitchClient(client_id, client_secret, request_interval=0.25)
        result = probe(client, args.game_id)
        stage = "output"
        rendered = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        summary = {key: result[key] for key in ("status", "checked_at", "game_id", "name", "hypes", "hypes_status")}
        print(json.dumps(summary, ensure_ascii=False, allow_nan=False))
        return 0
    except Exception as exc:
        print(json.dumps(_safe_error(exc, stage), ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
