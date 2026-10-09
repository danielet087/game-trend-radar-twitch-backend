"""Recheck published data inside the workflow's shared concurrency lock.

Only the standard library is used. Read immutable frontend blobs at the current
git HEAD so a cached branch URL cannot cause a second full collection.
"""
from __future__ import annotations

# Support both the historical module command and an absolute script path.
if __package__ in {None, ""}:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datetime import datetime, timezone
import json
import os
import re
import subprocess
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from radar_backend.domain.time import hour_slot, parse_time, validate_slot

FRONTEND = "danielet087/game-trend-radar"


def published_slot(payload: dict | None, *, receipt: bool, now: datetime) -> str | None:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise ValueError("Published JSON must be an object")
    if receipt:
        if payload.get("schema_version") != 1 or payload.get("collection_complete") is not True:
            raise ValueError("Invalid publication receipt")
        start = parse_time(payload.get("collection_started_at"))
        completed = parse_time(payload.get("completed_at"))
        if payload.get("generated_at") != payload.get("completed_at"):
            raise ValueError("Receipt completion timestamps disagree")
        observed = validate_slot(payload.get("observed_slot", ""))
        target = parse_time(validate_slot(payload.get("target_slot", "")))
        if observed != hour_slot(start) or target > start:
            raise ValueError("Receipt slot does not match actual collection")
    else:
        if payload.get("schema_version") != 2 or payload.get("coverage", {}).get("collection_complete") is not True:
            raise ValueError("Invalid latest collection")
        start = parse_time(payload.get("collection_started_at"))
        completed = parse_time(payload.get("generated_at"))
        observed = hour_slot(start)
    if start > completed or completed > now:
        raise ValueError("Invalid or future publication timestamps")
    return observed


def decide(*, now: datetime, requested_slot: str = "", source: str = "manual",
           force: bool = False, receipt: dict | None = None, latest: dict | None = None) -> dict:
    if source not in {"cloudflare", "schedule", "manual"}:
        raise ValueError("Unknown trigger source")
    if force and source != "manual":
        raise ValueError("Only an explicit manual run can force collection")
    current = hour_slot(now)
    target = validate_slot(requested_slot) if requested_slot else current
    if source == "cloudflare" and not requested_slot:
        raise ValueError("Cloudflare dispatch requires target_slot")
    result = {"should_collect": False, "target_slot": target, "trigger_source": source}
    # An old queued request must not pretend to measure a missed historical hour.
    if target != current:
        return {**result, "reason": "stale_or_future_slot"}
    observed = published_slot(receipt, receipt=True, now=now) if receipt is not None else published_slot(latest, receipt=False, now=now)
    if observed == current and not force:
        return {**result, "reason": "already_published"}
    return {**result, "should_collect": True, "reason": "manual_force" if force else "collection_due"}


def load_published() -> tuple[dict | None, dict | None]:
    remote = subprocess.run(
        ["git", "ls-remote", f"https://github.com/{FRONTEND}.git", "refs/heads/main"],
        check=True, capture_output=True, text=True, timeout=30,
    ).stdout.strip().split()
    if len(remote) != 2 or not re.fullmatch(r"[0-9a-f]{40}", remote[0]):
        raise ValueError("Cannot resolve current frontend HEAD")

    def read(path: str) -> dict | None:
        url = f"https://raw.githubusercontent.com/{FRONTEND}/{remote[0]}/{path}"
        try:
            with urlopen(Request(url, headers={"User-Agent": "game-trend-radar-collection-guard"}), timeout=20) as response:
                return json.load(response)
        except HTTPError as error:
            if error.code == 404:
                return None
            raise

    receipt = read("data/twitch_collection_status.json")
    return (receipt, None) if receipt is not None else (None, read("data/twitch_live.json"))


def main() -> None:
    event = os.environ.get("GITHUB_EVENT_NAME", "workflow_dispatch")
    source = "schedule" if event == "schedule" else os.environ.get("TRIGGER_SOURCE", "manual")
    receipt, latest = load_published()
    decision = decide(now=datetime.now(timezone.utc), requested_slot=os.environ.get("TARGET_SLOT", ""),
                      source=source, force=os.environ.get("FORCE_COLLECTION", "false") == "true",
                      receipt=receipt, latest=latest)
    print(json.dumps(decision))
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as output:
            for key, value in decision.items():
                output.write(f"{key}={str(value).lower() if isinstance(value, bool) else value}\n")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as summary:
            summary.write(f"### 每小時收集檢查\n\n- 時段（UTC）：{decision['target_slot']}\n"
                          f"- 來源：{source}\n- 判定：{decision['reason']}\n\n")


if __name__ == "__main__":
    main()
