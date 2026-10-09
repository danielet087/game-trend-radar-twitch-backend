"""Read, validate and persist a complete snapshot in the original write order."""

from __future__ import annotations

from pathlib import Path


def store_snapshot(
    payload: dict,
    frontend: Path,
    *,
    validate_snapshot_fn,
    parse_timestamp_fn,
    taipei,
    timestamp_fn,
    json_module,
    status_path_name,
    tracking_path_name,
    mapping_path_name,
    discovery_path_name,
    validate_receipt_fn,
    validate_persisted_tracking_fn,
    merge_tracking_state_fn,
    validate_persisted_mapping_fn,
    merge_mapping_state_fn,
    validate_persisted_discovery_fn,
    merge_discovery_state_fn,
    observation_order_fn,
    write_json_fn,
    deepcopy_fn,
    observed_rows_fn,
    history_observation_fn,
    latest_observation_fn,
    collection_receipt_fn,
) -> str:
    validate_snapshot_fn(payload)
    scheduled = "collection_schedule" in payload
    observed = parse_timestamp_fn(
        payload["collection_started_at"] if scheduled else payload["generated_at"]
    )
    completed = parse_timestamp_fn(payload["generated_at"])
    day = observed.astimezone(taipei).date().isoformat()
    hour = timestamp_fn(observed.replace(minute=0, second=0, microsecond=0))
    relative = f"data/twitch_history/{day}.json"
    history_path = frontend / relative
    history = (
        json_module.loads(history_path.read_text(encoding="utf-8"))
        if history_path.exists()
        else {"schema_version": 1, "date": day, "timezone": "Asia/Taipei", "hours": {}}
    )
    if (
        history.get("date") != day
        or history.get("schema_version") != 1
        or (not isinstance(history.get("hours"), dict))
    ):
        raise ValueError("Existing history is malformed; refusing to overwrite it")
    latest_path = frontend / "data/twitch_live.json"
    latest = (
        json_module.loads(latest_path.read_text(encoding="utf-8"))
        if latest_path.exists()
        else None
    )
    status_path = frontend / status_path_name
    status = (
        json_module.loads(status_path.read_text(encoding="utf-8"))
        if scheduled and status_path.exists()
        else None
    )
    status_order = validate_receipt_fn(status) if status is not None else None
    tracking_path = frontend / tracking_path_name
    tracking = (
        json_module.loads(tracking_path.read_text(encoding="utf-8"))
        if tracking_path.exists()
        else None
    )
    if tracking_path.exists():
        validate_persisted_tracking_fn(tracking)
    merged_tracking = (
        merge_tracking_state_fn(tracking, payload["tracking_state"])
        if "tracking_state" in payload
        else tracking
    )
    mapping_path = frontend / mapping_path_name
    mapping = (
        json_module.loads(mapping_path.read_text(encoding="utf-8"))
        if mapping_path.exists()
        else None
    )
    if mapping_path.exists():
        validate_persisted_mapping_fn(mapping)
    merged_mapping = (
        merge_mapping_state_fn(mapping, payload["steam_mapping_state"])
        if "steam_mapping_state" in payload
        else mapping
    )
    discovery_path = frontend / discovery_path_name
    discovery = (
        json_module.loads(discovery_path.read_text(encoding="utf-8"))
        if discovery_path.exists()
        else None
    )
    if discovery_path.exists():
        validate_persisted_discovery_fn(discovery)
    merged_discovery = (
        merge_discovery_state_fn(discovery, payload["steam_discovery_state"])
        if "steam_discovery_state" in payload
        else discovery
    )
    order = observation_order_fn(payload)
    update_latest = latest is None or order > observation_order_fn(latest)
    if status_order is not None and order < status_order:
        update_latest = False
    previous = history["hours"].get(hour)
    if not previous or order > observation_order_fn(previous):
        history["hours"][hour] = history_observation_fn(
            payload, scheduled, observed_rows_fn=observed_rows_fn
        )
        history["hours"] = dict(sorted(history["hours"].items()))
        write_json_fn(history, history_path)
    if update_latest:
        latest_payload = latest_observation_fn(
            payload,
            merged_tracking,
            merged_mapping,
            merged_discovery,
            deepcopy_fn=deepcopy_fn,
        )
        write_json_fn(latest_payload, latest_path)
    if merged_tracking is not None and merged_tracking != tracking:
        write_json_fn(merged_tracking, tracking_path)
    if merged_mapping is not None and merged_mapping != mapping:
        write_json_fn(merged_mapping, mapping_path)
    if merged_discovery is not None and merged_discovery != discovery:
        write_json_fn(merged_discovery, discovery_path)
    same_observation = latest is not None and {
        key: value
        for key, value in latest.items()
        if key not in {"tracking_state", "steam_mapping_state", "steam_discovery_state"}
    } == {
        key: value
        for key, value in payload.items()
        if key not in {"tracking_state", "steam_mapping_state", "steam_discovery_state"}
    }
    if (
        scheduled
        and (update_latest or same_observation)
        and (status_order is None or order >= status_order)
    ):
        receipt = collection_receipt_fn(
            payload, observed, completed, hour, relative, timestamp_fn=timestamp_fn
        )
        if receipt != status:
            write_json_fn(receipt, status_path)
    return relative
