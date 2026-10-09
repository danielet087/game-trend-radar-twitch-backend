"""Pure snapshot validation, race merges and history projection with explicit ports."""

from __future__ import annotations

from datetime import datetime


def merge_discovery_state(
    existing: dict | None,
    incoming: dict,
    *,
    validate_persisted_discovery_fn,
    datetime_type,
    timezone_value,
    parse_timestamp_fn,
    timestamp_fn,
    deepcopy_fn,
) -> dict:
    """Union intake provenance while keeping the latest successful ID decision.

    Public catalog membership changes on every metadata pass, independently of
    the cached IGDB lookup. Failed lookups have no new authoritative checked_at.
    """
    new = validate_persisted_discovery_fn(incoming)
    if existing is None:
        return new
    old = validate_persisted_discovery_fn(existing)
    floor = datetime_type.min.replace(tzinfo=timezone_value.utc)

    def clock(state):
        return (
            parse_timestamp_fn(state["updated_at"])
            if state.get("updated_at")
            else floor
        )

    def identity_clock(entry):
        return (
            parse_timestamp_fn(entry["checked_at"])
            if entry.get("checked_at")
            else floor
        )

    def metadata_clock(entry, state):
        return (
            parse_timestamp_fn(entry["updated_at"])
            if entry.get("updated_at")
            else clock(state)
        )

    result = deepcopy_fn(new if clock(new) >= clock(old) else old)
    for key in ("steam_source_id", "twitch_source_id"):
        sources = {state[key] for state in (old, new) if state.get(key) is not None}
        if len(sources) > 1:
            raise ValueError("Conflicting official discovery source identities")
        if sources:
            result[key] = sources.pop()
    result["games"] = {}
    catalog_states = [
        state for state in (old, new) if isinstance(state.get("source_catalog"), dict)
    ]
    public_ids = None
    if catalog_states:
        catalog_state = max(
            catalog_states,
            key=lambda state: (
                parse_timestamp_fn(state["source_catalog"]["generated_at"]),
                clock(state),
            ),
        )
        result["source_catalog"] = deepcopy_fn(catalog_state["source_catalog"])
        public_ids = set(result["source_catalog"]["appids"])
    for game_id in sorted(set(old["games"]) | set(new["games"])):
        previous, current = (old["games"].get(game_id), new["games"].get(game_id))
        if previous is None or current is None:
            merged = deepcopy_fn(current if previous is None else previous)
            if public_ids is not None:
                merged["public_steam_appids"] = [
                    appid
                    for appid in merged.get("steam_appids", [])
                    if appid in public_ids
                ]
                merged["missing_public_appids"] = [
                    appid
                    for appid in merged.get("steam_appids", [])
                    if appid not in public_ids
                ]
            result["games"][game_id] = merged
            continue
        old_order = (identity_clock(previous), metadata_clock(previous, old))
        new_order = (identity_clock(current), metadata_clock(current, new))
        merged = deepcopy_fn(current if new_order >= old_order else previous)
        metadata = (
            current
            if metadata_clock(current, new) >= metadata_clock(previous, old)
            else previous
        )
        for key in (
            "active",
            "public_steam_appids",
            "missing_public_appids",
            "twitch_enrollment",
        ):
            if key in metadata:
                merged[key] = deepcopy_fn(metadata[key])
        merged["updated_at"] = timestamp_fn(
            metadata_clock(metadata, new if metadata is current else old)
        )
        seen = [
            entry["first_seen_at"]
            for entry in (previous, current)
            if entry.get("first_seen_at")
        ]
        if seen:
            merged["first_seen_at"] = min(seen, key=parse_timestamp_fn)
        enrollments = [
            entry["twitch_enrollment"]
            for entry in (previous, current)
            if isinstance(entry.get("twitch_enrollment"), dict)
            and entry["twitch_enrollment"].get("observed_at")
        ]
        if enrollments:
            merged["twitch_enrollment"] = deepcopy_fn(
                min(
                    enrollments,
                    key=lambda value: parse_timestamp_fn(value["observed_at"]),
                )
            )
        same_identity = [
            entry
            for entry in (previous, current)
            if entry.get("igdb_id") == merged.get("igdb_id")
        ]
        versions = [entry.get("lookup_policy_version", 1) for entry in same_identity]
        if any(("lookup_policy_version" in entry for entry in same_identity)):
            merged["lookup_policy_version"] = max(versions)
        related_keys = (
            "related_steam_identity",
            "related_lookup_status",
            "related_checked_at",
            "related_retry_at",
        )
        for key in related_keys:
            merged.pop(key, None)
        if merged.get("status") == "no_steam_link" and merged.get("igdb_id"):
            eligible = [
                entry
                for entry in same_identity
                if entry.get("status") == "no_steam_link"
            ]
            decisions = []
            for entry in eligible:
                proof = entry.get("related_steam_identity")
                checked = entry.get("related_checked_at") or (proof or {}).get(
                    "checked_at"
                )
                if checked and (
                    proof or entry.get("related_lookup_status") == "no_steam_website"
                ):
                    decisions.append(
                        (
                            parse_timestamp_fn(checked),
                            metadata_clock(entry, new if entry is current else old),
                            entry,
                        )
                    )
            if decisions:
                checked, _, decision = max(decisions, key=lambda item: item[:2])
                for key in related_keys:
                    if key in decision:
                        merged[key] = deepcopy_fn(decision[key])
                merged["related_checked_at"] = timestamp_fn(checked)
                merged["related_lookup_status"] = (
                    "matched"
                    if merged.get("related_steam_identity")
                    else "no_steam_website"
                )
            operations = [
                entry
                for entry in eligible
                if entry.get("related_lookup_status") is not None
            ]
            if operations:
                latest = max(
                    operations,
                    key=lambda entry: metadata_clock(
                        entry, new if entry is current else old
                    ),
                )
                latest_at = metadata_clock(latest, new if latest is current else old)
                decision_at = max((item[0] for item in decisions), default=floor)
                if (
                    latest.get("related_lookup_status") == "unavailable"
                    and latest_at >= decision_at
                ):
                    merged.update(
                        related_lookup_status="unavailable", related_retry_at=None
                    )
        catalog_ids = (
            public_ids
            if public_ids is not None
            else set(metadata.get("public_steam_appids", []))
        )
        merged["public_steam_appids"] = [
            appid for appid in merged.get("steam_appids", []) if appid in catalog_ids
        ]
        merged["missing_public_appids"] = [
            appid
            for appid in merged.get("steam_appids", [])
            if appid not in catalog_ids
        ]
        result["games"][game_id] = merged
    return validate_persisted_discovery_fn(result)


def merge_mapping_state(
    existing: dict | None,
    incoming: dict,
    *,
    validate_persisted_mapping_fn,
    datetime_type,
    timezone_value,
    parse_timestamp_fn,
    timestamp_fn,
    deepcopy_fn,
) -> dict:
    """Keep all AppID mappings while protecting newer mapping decisions."""
    new = validate_persisted_mapping_fn(incoming)
    if existing is None:
        return new
    old = validate_persisted_mapping_fn(existing)
    floor = datetime_type.min.replace(tzinfo=timezone_value.utc)
    clock = lambda state: (
        parse_timestamp_fn(state["updated_at"]) if state.get("updated_at") else floor
    )
    result = deepcopy_fn(new if clock(new) >= clock(old) else old)
    result["games"] = {}
    for appid in sorted(set(old["games"]) | set(new["games"])):
        previous, current = (old["games"].get(appid), new["games"].get(appid))
        if previous is None or current is None:
            result["games"][appid] = deepcopy_fn(
                current if previous is None else previous
            )
            continue
        entry_clock = lambda entry, state: (
            (
                parse_timestamp_fn(entry["checked_at"])
                if entry.get("checked_at")
                else clock(state)
            ),
            clock(state),
        )
        merged = deepcopy_fn(
            current
            if entry_clock(current, new) >= entry_clock(previous, old)
            else previous
        )
        metadata_clock = lambda entry, state: (
            parse_timestamp_fn(entry["metadata_updated_at"])
            if entry.get("metadata_updated_at")
            else clock(state)
        )
        metadata = (
            current
            if metadata_clock(current, new) >= metadata_clock(previous, old)
            else previous
        )
        merged["steam"] = deepcopy_fn(metadata["steam"])
        merged["metadata_updated_at"] = timestamp_fn(
            metadata_clock(metadata, new if metadata is current else old)
        )
        result["games"][appid] = merged
    return validate_persisted_mapping_fn(result)


def merge_tracking_state(
    existing: dict | None,
    incoming: dict,
    *,
    validate_persisted_tracking_fn,
    parse_timestamp_fn,
    deepcopy_fn,
    timestamp_fn,
    normalize_tracking_state_fn,
) -> dict:
    """Union enrollments when publishing against a newer frontend checkout.

    A late result may contribute a previously unknown enrollment, but cannot
    rewind an entry's metadata, status or most recent real observation. A
    terminal status is not deleted: it remains a tombstone for later readers.
    """
    new = validate_persisted_tracking_fn(incoming)
    if existing is None:
        return new
    old = validate_persisted_tracking_fn(existing)
    new_time, old_time = (
        parse_timestamp_fn(state["updated_at"]) for state in (new, old)
    )
    result = deepcopy_fn(new if new_time >= old_time else old)
    result["games"] = {}
    for game_id in sorted(set(old["games"]) | set(new["games"])):
        previous, current = (old["games"].get(game_id), new["games"].get(game_id))
        if previous is None or current is None:
            result["games"][game_id] = deepcopy_fn(
                current if previous is None else previous
            )
            continue
        old_updated = parse_timestamp_fn(
            previous.get("updated_at") or old["updated_at"]
        )
        new_updated = parse_timestamp_fn(current.get("updated_at") or new["updated_at"])
        merged = deepcopy_fn(current if new_updated >= old_updated else previous)
        source_states = {}
        for source_key in set(previous.get("tracking_sources", {})) | set(
            current.get("tracking_sources", {})
        ):
            old_source = previous.get("tracking_sources", {}).get(source_key)
            new_source = current.get("tracking_sources", {}).get(source_key)
            if old_source is None or new_source is None:
                source = deepcopy_fn(new_source if old_source is None else old_source)
            else:
                source_clock = lambda value, fallback: parse_timestamp_fn(
                    value.get("updated_at") or timestamp_fn(fallback)
                )
                source = deepcopy_fn(
                    new_source
                    if source_clock(new_source, new_updated)
                    >= source_clock(old_source, old_updated)
                    else old_source
                )
                values = [
                    value["first_seen_at"]
                    for value in (old_source, new_source)
                    if value.get("first_seen_at")
                ]
                if values:
                    source["first_seen_at"] = min(values, key=parse_timestamp_fn)
                enrollments = [
                    value["enrollment"]
                    for value in (old_source, new_source)
                    if isinstance(value.get("enrollment"), dict)
                    and value["enrollment"].get("observed_at")
                ]
                if enrollments:
                    source["enrollment"] = deepcopy_fn(
                        min(
                            enrollments,
                            key=lambda item: parse_timestamp_fn(item["observed_at"]),
                        )
                    )
            source_states[source_key] = source
        if source_states:
            merged["tracking_sources"] = source_states
        for field in ("first_seen_at", "enrolled_at"):
            values = [entry[field] for entry in (previous, current) if entry.get(field)]
            if values:
                merged[field] = min(values, key=parse_timestamp_fn)
        enrollments = [
            entry["enrollment"]
            for entry in (previous, current)
            if isinstance(entry.get("enrollment"), dict)
            and entry["enrollment"].get("observed_at")
        ]
        if enrollments:
            merged["enrollment"] = deepcopy_fn(
                min(
                    enrollments,
                    key=lambda item: parse_timestamp_fn(item["observed_at"]),
                )
            )
        observations = [
            entry["last_observation"]
            for entry in (previous, current)
            if isinstance(entry.get("last_observation"), dict)
        ]
        if observations:
            merged["last_observation"] = deepcopy_fn(
                max(
                    observations,
                    key=lambda item: parse_timestamp_fn(item["observation_at"]),
                )
            )
        seen_at = [
            entry["last_seen_at"]
            for entry in (previous, current)
            if entry.get("last_seen_at")
        ]
        if seen_at:
            merged["last_seen_at"] = max(seen_at, key=parse_timestamp_fn)
        result["games"][game_id] = merged
    return normalize_tracking_state_fn(result, now=max(new_time, old_time))


def observed_rows(payload: dict) -> list[dict]:
    """Deduplicate discovery and enrolled games; only real observations belong in history."""
    rows = {row["game_id"]: row for row in payload["candidate_games"]}
    for row in payload.get("tracked_games", []):
        previous = rows.get(row["game_id"])
        if previous is not None and any(
            (
                previous.get(field) != row.get(field)
                for field in (
                    "viewer_count",
                    "streamer_count",
                    "median_viewer_count",
                    "measurement_started_at",
                    "measurement_finished_at",
                    "filtered_audience",
                )
            )
        ):
            raise ValueError("Candidate and tracked observations disagree")
        rows[row["game_id"]] = row
    return list(rows.values())


def validate_schedule(
    payload: dict, *, parse_timestamp_fn, timestamp_fn
) -> dict | None:
    """New collectors use real observation time; old published hours stay intact."""
    if "collection_schedule" not in payload:
        return None
    schedule = payload["collection_schedule"]
    if not isinstance(schedule, dict):
        raise ValueError("Invalid collection schedule metadata")
    target = parse_timestamp_fn(schedule.get("target_slot"))
    if schedule["target_slot"] != timestamp_fn(
        target.replace(minute=0, second=0, microsecond=0)
    ):
        raise ValueError("Target slot must be a canonical UTC hour")
    if schedule.get("trigger_source") not in {"cloudflare", "schedule", "manual"}:
        raise ValueError("Invalid collection trigger source")
    for key in ("run_id", "run_attempt"):
        value = schedule.get(key)
        if (
            not isinstance(value, str)
            or not value.isascii()
            or (not value.isdigit())
            or (int(value) < 1)
        ):
            raise ValueError(f"Invalid collection {key}")
    started = parse_timestamp_fn(payload.get("collection_started_at"))
    completed = parse_timestamp_fn(payload.get("generated_at"))
    if not target <= started <= completed:
        raise ValueError("Target, collection start and completion are out of order")
    return schedule


def observation_order(
    payload: dict, *, parse_timestamp_fn
) -> tuple[datetime, datetime]:
    """A late-published older sample must not replace a newer observation."""
    completed = parse_timestamp_fn(payload["generated_at"])
    observed = (
        parse_timestamp_fn(payload["collection_started_at"])
        if "collection_schedule" in payload
        else completed
    )
    return (observed, completed)


def validate_receipt(
    receipt: dict, *, parse_timestamp_fn, timestamp_fn, taipei
) -> tuple[datetime, datetime]:
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema_version") != 1
        or receipt.get("collection_complete") is not True
    ):
        raise ValueError(
            "Existing collection status is malformed; refusing to overwrite it"
        )
    started, completed = (
        parse_timestamp_fn(receipt.get(key))
        for key in ("collection_started_at", "completed_at")
    )
    observed_slot = timestamp_fn(started.replace(minute=0, second=0, microsecond=0))
    expected_history = (
        f"data/twitch_history/{started.astimezone(taipei).date().isoformat()}.json"
    )
    if (
        started > completed
        or receipt.get("observed_slot") != observed_slot
        or receipt.get("generated_at") != timestamp_fn(completed)
        or (receipt.get("history_path") != expected_history)
    ):
        raise ValueError(
            "Existing collection status has inconsistent observation times"
        )
    target = parse_timestamp_fn(receipt.get("target_slot"))
    if (
        receipt["target_slot"]
        != timestamp_fn(target.replace(minute=0, second=0, microsecond=0))
        or target > started
    ):
        raise ValueError("Existing collection status has an invalid target slot")
    return (started, completed)


def validate_filtered_audience(
    value: dict, *, viewers: int, streamers: int, math_module
) -> None:
    """Keep the new population explicit; incomplete lookups cannot claim a median."""
    if (
        not isinstance(value, dict)
        or value.get("rule") != "followers_gt_1000_viewers_gte_10_v1"
    ):
        raise ValueError("Invalid filtered audience rule")
    for key, expected in (
        ("min_followers_exclusive", 1000),
        ("min_viewers_inclusive", 10),
        ("followers_max_age_hours", 24),
    ):
        if type(value.get(key)) is not int or value[key] != expected:
            raise ValueError("Invalid filtered audience threshold")
    partitions = (
        "eligible_streamer_count",
        "excluded_low_viewer_count",
        "excluded_low_follower_count",
        "unknown_follower_count",
    )
    if any((type(value.get(key)) is not int or value[key] < 0 for key in partitions)):
        raise ValueError("Invalid filtered audience counts")
    if sum((value[key] for key in partitions)) != streamers:
        raise ValueError("Filtered audience counts do not match the census")
    eligible, unknown = (
        value["eligible_streamer_count"],
        value["unknown_follower_count"],
    )
    total, middle = (
        value.get("eligible_viewer_count"),
        value.get("median_viewer_count"),
    )
    if (
        type(total) is not int
        or not eligible * 10 <= total <= viewers
        or (eligible == 0 and total != 0)
    ):
        raise ValueError("Invalid filtered audience viewer total")
    if value.get("status") != ("partial" if unknown else "complete"):
        raise ValueError("Filtered audience status does not match coverage")
    if unknown or eligible == 0:
        if middle is not None:
            raise ValueError("An incomplete or empty audience cannot claim a median")
    elif (
        type(middle) not in (int, float)
        or not math_module.isfinite(middle)
        or (not 10 <= middle <= total)
    ):
        raise ValueError("Invalid filtered audience median")


def validate_snapshot(
    payload: dict,
    *,
    parse_timestamp_fn,
    validate_schedule_fn,
    validate_persisted_tracking_fn,
    validate_persisted_mapping_fn,
    validate_persisted_discovery_fn,
    observed_rows_fn,
    math_module,
    validate_filtered_audience_fn,
) -> None:
    if (
        payload.get("schema_version") != 2
        or payload.get("coverage", {}).get("collection_complete") is not True
    ):
        raise ValueError("Only complete schema-v2 snapshots may be published")
    parse_timestamp_fn(payload["generated_at"])
    schedule = validate_schedule_fn(payload)
    if type(payload.get("min_viewers")) is not int or payload["min_viewers"] < 1:
        raise ValueError("Invalid threshold")
    if not isinstance(payload.get("candidate_games"), list):
        raise ValueError("Missing candidate list")
    tracked = payload.get("tracked_games", [])
    if not isinstance(tracked, list):
        raise ValueError("Invalid tracked game list")
    tracking = (
        validate_persisted_tracking_fn(payload["tracking_state"])
        if "tracking_state" in payload
        else None
    )
    if "steam_mapping_state" in payload:
        validate_persisted_mapping_fn(payload["steam_mapping_state"])
    if "steam_discovery_state" in payload:
        validate_persisted_discovery_fn(payload["steam_discovery_state"])
    if tracked and tracking is None:
        raise ValueError("Tracked observations require their persistent registry")
    for row in tracked:
        entry = tracking["games"].get(row.get("game_id"), {})
        if (
            entry.get("status") != "active"
            or row.get("observation_status") != "current"
        ):
            raise ValueError(
                "Only active, freshly observed tracked games may be published"
            )
    candidate_ids = {row.get("game_id") for row in payload["candidate_games"]}
    for group in (payload["candidate_games"], tracked):
        ids = [row.get("game_id") for row in group]
        if len(set(ids)) != len(ids):
            raise ValueError("Invalid or duplicate game ID")
    seen = set()
    for row in observed_rows_fn(payload):
        game_id = row["game_id"]
        if not isinstance(game_id, str) or not game_id.isdigit() or game_id in seen:
            raise ValueError("Invalid or duplicate game ID")
        seen.add(game_id)
        allowed_statuses = (
            {"new", "pending"}
            if game_id in candidate_ids
            else {"new", "pending", "not_new"}
        )
        if (
            row.get("pagination_complete") is not True
            or row.get("verification", {}).get("status") not in allowed_statuses
        ):
            raise ValueError("Unfinished or excluded category in candidate list")
        viewers, streamers, middle = (
            row.get("viewer_count"),
            row.get("streamer_count"),
            row.get("median_viewer_count"),
        )
        minimum = payload["min_viewers"] if game_id in candidate_ids else 0
        if (
            type(viewers) is not int
            or viewers < minimum
            or type(streamers) is not int
            or (streamers < 0)
        ):
            raise ValueError("Invalid candidate metrics")
        if streamers == 0:
            if game_id in candidate_ids or viewers != 0 or middle is not None:
                raise ValueError(
                    "An empty stream census must have zero viewers and no median"
                )
        elif (
            type(middle) not in (float, int)
            or not math_module.isfinite(middle)
            or (not 0 <= middle <= viewers)
        ):
            raise ValueError("Invalid median")
        if "filtered_audience" in row:
            validate_filtered_audience_fn(
                row["filtered_audience"], viewers=viewers, streamers=streamers
            )
        if schedule is not None:
            started, completed = (
                parse_timestamp_fn(payload[key])
                for key in ("collection_started_at", "generated_at")
            )
            measured_start, measured_finish = (
                parse_timestamp_fn(row.get(key))
                for key in ("measurement_started_at", "measurement_finished_at")
            )
            if not started <= measured_start <= measured_finish <= completed:
                raise ValueError(
                    "Category measurement is outside the collection window"
                )


def history_observation(payload: dict, scheduled: bool, *, observed_rows_fn) -> dict:
    """Project complete observations without dropping optional release evidence."""
    return {
        "generated_at": payload["generated_at"],
        "collection_started_at": payload["collection_started_at"],
        "min_viewers": payload["min_viewers"],
        "stop_reason": payload["coverage"]["stop_reason"],
        **(
            {"collection_schedule": dict(payload["collection_schedule"])}
            if scheduled
            else {}
        ),
        "games": [
            {
                **{
                    key: row[key]
                    for key in (
                        "game_id",
                        "game_name",
                        "viewer_count",
                        "streamer_count",
                        "median_viewer_count",
                        "measurement_started_at",
                        "measurement_finished_at",
                        "verification",
                    )
                },
                **{
                    key: row[key]
                    for key in (
                        "release_experiment",
                        "filtered_audience",
                        "tracking",
                        "steam_matches",
                        "observation_status",
                        "observation_at",
                    )
                    if key in row
                },
            }
            for row in observed_rows_fn(payload)
        ],
    }


def latest_observation(
    payload: dict,
    merged_tracking: dict | None,
    merged_mapping: dict | None,
    merged_discovery: dict | None,
    *,
    deepcopy_fn,
) -> dict:
    """Copy the frozen result and embed the independently merged registries."""
    latest_payload = deepcopy_fn(payload)
    if merged_tracking is not None and "tracking_state" in payload:
        latest_payload["tracking_state"] = merged_tracking
    if merged_mapping is not None and "steam_mapping_state" in payload:
        latest_payload["steam_mapping_state"] = merged_mapping
    if merged_discovery is not None and "steam_discovery_state" in payload:
        latest_payload["steam_discovery_state"] = merged_discovery
    return latest_payload


def collection_receipt(
    payload: dict,
    observed: datetime,
    completed: datetime,
    hour: str,
    relative: str,
    *,
    timestamp_fn,
) -> dict:
    """Record the real observation hour, including delayed trigger provenance."""
    return {
        "schema_version": 1,
        "observed_slot": hour,
        "target_slot": payload["collection_schedule"]["target_slot"],
        "collection_started_at": timestamp_fn(observed),
        "completed_at": timestamp_fn(completed),
        "generated_at": timestamp_fn(completed),
        "collection_complete": True,
        "run_id": payload["collection_schedule"]["run_id"],
        "history_path": relative,
    }
