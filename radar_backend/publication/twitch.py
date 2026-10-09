"""Merge a frozen census into the latest frontend using the shared Git engine."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any

from radar_core.publication import (
    PublicationReceipt, SubprocessGitRepository, publish_with_retry, snapshot_revision,
)
from radar_backend.state.json_snapshot import write_json
from scripts.store_twitch_snapshot import (
    DISCOVERY_PATH, MAPPING_PATH, STATUS_PATH, TRACKING_PATH,
    observation_order, parse_timestamp, store_snapshot, timestamp, validate_snapshot,
)

PUBLICATION_PATHS = (
    "data/twitch_live.json", "data/twitch_history/",
    STATUS_PATH, TRACKING_PATH, MAPPING_PATH, DISCOVERY_PATH,
)


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON contains duplicate keys")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise ValueError("JSON contains a non-finite number")


def _strict_json(path: Path) -> Any:
    document = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_unique_keys, parse_constant=_reject_constant,
    )
    # Exponent overflow (e.g. 1e999) is parsed as a float rather than a JSON
    # constant; canonical validation also rejects that non-finite value.
    snapshot_revision(document)
    return document


def _preflight_existing_json(frontend: Path) -> None:
    """Reject ambiguous destination data before a merge can rewrite its proof."""
    for relative in PUBLICATION_PATHS:
        path = frontend / relative
        current = path
        while current != frontend:
            if current.is_symlink():
                raise ValueError("Published JSON cannot use symlinks")
            current = current.parent
        if not path.exists():
            continue
        if path.is_dir():
            for item in path.rglob("*"):
                if item.is_symlink():
                    raise ValueError("Published JSON cannot use symlinks")
                if item.is_file() and item.suffix == ".json":
                    _strict_json(item)
        else:
            _strict_json(path)


def freeze_snapshot(source: str | Path) -> dict[str, Any]:
    """Read and validate once, before any destination checkout is refreshed."""
    payload = _strict_json(Path(source))
    if not isinstance(payload, dict):
        raise ValueError("Collected snapshot must be an object")
    validate_snapshot(payload)
    input_revision(payload)
    snapshot_revision(payload)
    return payload


def input_revision(payload: dict[str, Any]) -> str:
    """Old recovery snapshots retain a content revision without invented inputs."""
    if "input_revision" not in payload:
        return snapshot_revision(payload)
    revision = payload["input_revision"]
    if (not isinstance(revision, str) or len(revision) != 64
            or any(character not in "0123456789abcdef" for character in revision)):
        raise ValueError("Invalid collected input revision")
    return revision


def input_kind(payload: dict[str, Any]) -> str:
    return "collection_inputs" if "input_revision" in payload else "legacy_collected_snapshot"


def _census(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items()
            if key not in {"tracking_state", "steam_mapping_state", "steam_discovery_state"}}


def merge_frozen_snapshot(
    payload: dict[str, Any], frontend: Path, *, source_revision: str, payload_revision: str,
) -> None:
    """Keep existing observation ordering and all independent enrollment sources."""
    _preflight_existing_json(frontend)
    latest_path = frontend / "data/twitch_live.json"
    if latest_path.exists():
        latest = json.loads(latest_path.read_text(encoding="utf-8"))
        if observation_order(latest) == observation_order(payload) and _census(latest) != _census(payload):
            raise ValueError("Conflicting Twitch census for the same observation time")
    store_snapshot(deepcopy(payload), frontend)
    if "collection_schedule" not in payload:
        return
    status_path = frontend / STATUS_PATH
    if not status_path.exists():
        return
    receipt = json.loads(status_path.read_text(encoding="utf-8"))
    latest = json.loads(latest_path.read_text(encoding="utf-8"))
    if _census(latest) != _census(payload):
        return
    schedule = payload["collection_schedule"]
    # An old sample may fill history and recover enrollment provenance. It must
    # never attach its revisions to a newer census or claim that newer slot.
    if any(receipt.get(key) != value for key, value in (
        ("collection_started_at", timestamp(parse_timestamp(payload["collection_started_at"]))),
        ("completed_at", timestamp(parse_timestamp(payload["generated_at"]))),
        ("target_slot", schedule["target_slot"]),
        ("run_id", schedule["run_id"]),
    )):
        return
    annotated = {**receipt, "input_revision": source_revision,
                 "input_kind": input_kind(payload), "payload_revision": payload_revision}
    if annotated != receipt:
        write_json(annotated, status_path)


def publish_snapshot(
    payload: dict[str, Any], repository: SubprocessGitRepository, *, max_attempts: int = 5,
) -> PublicationReceipt:
    """Freeze the source so retries perform only fresh-state merge and Git IO."""
    frozen = deepcopy(payload)
    validate_snapshot(frozen)
    source_revision = input_revision(frozen)
    payload_revision = snapshot_revision(frozen)
    return publish_with_retry(
        repository,
        lambda frontend: merge_frozen_snapshot(
            frozen, frontend, source_revision=source_revision, payload_revision=payload_revision,
        ),
        paths=PUBLICATION_PATHS,
        message="data: update twitch live metrics",
        input_revision=source_revision,
        payload_revision=payload_revision,
        max_attempts=max_attempts,
    )
