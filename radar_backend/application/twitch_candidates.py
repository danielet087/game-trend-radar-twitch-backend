"""Bounded category enumeration with explicit clock, source, state and audience ports."""

from __future__ import annotations
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


def initialize_page_reader(
    self,
    client: Any,
    max_calls: int,
    *,
    deadline: float | None = None,
    monotonic: Callable[[], float],
):
    self.client, self.max_calls, self.calls = (client, max_calls, 0)
    self.deadline, self.monotonic = (deadline, monotonic)


def read_page(
    self,
    endpoint: str,
    params: dict,
    *,
    incomplete_error,
    deadline_error,
) -> tuple[list[dict], str | None]:
    if self.deadline is not None and self.monotonic() >= self.deadline:
        raise incomplete_error(
            "collection_deadline_exhausted during census; nothing will be published"
        )
    if self.calls >= self.max_calls:
        raise incomplete_error("Helix call budget exhausted; nothing will be published")
    self.calls += 1
    try:
        payload = self.client.get(endpoint, params=params)
    except deadline_error:
        raise incomplete_error(
            "collection_deadline_exhausted during census; nothing will be published"
        ) from None
    if self.deadline is not None and self.monotonic() >= self.deadline:
        raise incomplete_error(
            "collection_deadline_exhausted during census; nothing will be published"
        )
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise incomplete_error(f"Malformed Helix {endpoint} response")
    rows = payload["data"]
    pagination = payload.get("pagination", {})
    if not all((isinstance(row, dict) for row in rows)) or not isinstance(pagination, dict):
        raise incomplete_error(f"Malformed Helix {endpoint} page")
    cursor = pagination.get("cursor")
    if cursor is not None and (not isinstance(cursor, str)):
        raise incomplete_error("Malformed pagination cursor")
    return (rows, cursor or None)


def category_metrics(
    reader: Any,
    game_id: str,
    max_pages: int = 150,
    *,
    retain_channels: bool = False,
    utcnow,
    timestamp,
    accumulate_streams,
    census_metrics,
    counter_type,
    median_fn,
    incomplete_error,
) -> dict:
    channels: dict[str, dict] = {}
    seen_cursors: set[str] = set()
    after = None
    duplicates = 0
    started = timestamp(utcnow())
    for page_index in range(max_pages):
        params = {"game_id": game_id, "type": "live", "first": 100}
        if after:
            params["after"] = after
        streams, cursor = reader.get("streams", params)
        # Broadcaster rows are counted once; the latest page observation wins.
        duplicates += accumulate_streams(
            channels, streams, game_id, incomplete_error=incomplete_error
        )
        if not cursor:
            break
        if cursor in seen_cursors:
            raise incomplete_error(f"Repeated stream cursor for game {game_id}")
        seen_cursors.add(cursor)
        after = cursor
    else:
        raise incomplete_error(f"Stream page limit reached for game {game_id}")
    result = census_metrics(channels, counter_type=counter_type, median_fn=median_fn)
    result.update(
        {
            "measurement_started_at": started,
            "measurement_finished_at": timestamp(utcnow()),
            "pagination_complete": True,
            "stream_pages": page_index + 1,
            "duplicate_broadcasters_removed": duplicates,
        }
    )
    if retain_channels:
        result["_channels"] = list(channels.values())
    return result


def collect_candidates(
    *,
    client_id: str,
    client_secret: str,
    min_viewers: int,
    max_category_pages: int,
    max_stream_pages: int,
    max_api_calls: int,
    registry_path: str | Path,
    include_release_hints: bool,
    release_dates_path: str | Path,
    client: Any | None,
    now: datetime | None,
    category_page_size: int,
    include_filtered_audience: bool,
    followers_cache_path: str | Path,
    followers_max_calls: int | None,
    followers_max_seconds: float | None,
    max_collection_seconds: float,
    monotonic: Callable[[], float],
    tracking_state: dict | None,
    steam_catalog: dict | None,
    steam_mapping_state: dict | None,
    steam_discovery_state: dict | None,
    datetime_type,
    timezone_type,
    math_module,
    normalize_tracking_state,
    load_verifications,
    load_release_dates,
    client_type,
    page_reader,
    mapping_callbacks,
    reconcile_steam_catalog,
    evaluate_date,
    reconcile_tracking_entry,
    timestamp,
    non_game_ids,
    release_hints,
    verification_for,
    category_metrics,
    logger,
    attach_experiments,
    follower_resolver,
    attach_filtered_audience,
    enroll_observation,
    parse_timestamp,
    discovery_callback,
    incomplete_error,
) -> dict[str, Any]:
    collection_started = monotonic()
    if not math_module.isfinite(max_collection_seconds) or max_collection_seconds <= 0:
        raise ValueError("Collection time budget must be positive and finite")
    deadline = collection_started + max_collection_seconds
    if (
        min(min_viewers, max_category_pages, max_stream_pages, max_api_calls) < 1
        or not 1 <= category_page_size <= 100
    ):
        raise ValueError("Threshold and limits must be positive; page size must be 1..100")
    clock = now or datetime_type.now(timezone_type.utc)
    tracking = normalize_tracking_state(tracking_state, clock, non_game_ids=non_game_ids)
    observations = load_verifications(registry_path)
    release_dates = load_release_dates(release_dates_path)
    client = client or client_type(client_id, client_secret, request_interval=0.3)
    if isinstance(client, client_type):
        client.collection_deadline, client.monotonic = (deadline, monotonic)
    reader = page_reader(client, max_api_calls, deadline=deadline, monotonic=monotonic)
    normalize_mapping_state, normalize_steam_catalog, refresh_mappings = mapping_callbacks()
    mappings = normalize_mapping_state(steam_mapping_state)
    steam_summary = {"status": "not_supplied", "catalog_games": None, "recent_releases": None}
    steam_matches_by_id = None
    if steam_catalog is not None:
        catalog = normalize_steam_catalog(steam_catalog, clock)
        mappings = refresh_mappings(
            client,
            steam_catalog,
            mappings,
            clock,
            deadline=deadline,
            monotonic=monotonic,
            discovery_state=steam_discovery_state,
            tracking_state=tracking,
        )
        reconcile_steam_catalog(tracking, catalog, mappings, clock, non_game_ids=non_game_ids)
        steam_by_appid = {steam["steam_appid"]: steam for steam in catalog}
        steam_matches_by_id = {}
        for appid, mapping in mappings["games"].items():
            if mapping.get("status") == "matched" and appid in steam_by_appid:
                steam_matches_by_id.setdefault(str(mapping["twitch_game_id"]), []).append(
                    steam_by_appid[appid]
                )
        steam_summary = {
            "status": "ok",
            "catalog_games": len(catalog),
            "recent_releases": sum((bool(steam.get("is_recent")) for steam in catalog)),
            "matched_games": sum(
                (row.get("status") == "matched" for row in mappings["games"].values())
            ),
            "mapping_report": mappings.get("report"),
        }
    candidates_by_id, excluded_by_id = ({}, {})
    retained_by_id = {}
    channels_by_game: dict[str, list[dict]] = {}
    seen_ids, seen_cursors = (set(), set())
    measured_ids = set()
    below_ids = set()
    hints, requested_igdb_ids, hints_statuses = ({}, set(), [])
    igdb_predictions = {}
    after = None
    measured_count = below_count = 0
    stop_reason = None

    def refresh_tracking(game: dict, prediction: dict, at: datetime) -> dict | None:
        entry = tracking["games"].get(str(game["id"]))
        if entry is None:
            return None
        for source_key, target_key in (
            ("name", "game_name"),
            ("igdb_id", "igdb_id"),
            ("box_art_url", "box_art_url"),
        ):
            if game.get(source_key):
                entry[target_key] = game[source_key]
        released = prediction.get("release_at") if prediction.get("status") == "evaluated" else None
        release_source = "igdb_first_release_date" if released else None
        if not released:
            dated = release_dates.get(str(game["id"]), {})
            trial = evaluate_date(
                dated.get("original_release_date"),
                dated.get("observed_at"),
                at,
                source="twitch_original_release_date",
            )
            if trial.get("status") == "evaluated":
                released, release_source = (trial.get("release_at"), "twitch_original_release_date")
        reconcile_tracking_entry(
            entry, at, release_at=released, release_source=release_source, non_game_ids=non_game_ids
        )
        entry["updated_at"] = timestamp(at)
        return entry

    for page_index in range(max_category_pages):
        params: dict[str, Any] = {"first": category_page_size}
        if after:
            params["after"] = after
        games, cursor = reader.get("games/top", params)
        if cursor and cursor in seen_cursors:
            raise incomplete_error("Repeated category cursor")
        # Failed or missing metadata remains unknown and cannot exclude a game.
        for game in games:
            if (
                not str(game.get("id") or "").isdigit()
                or not isinstance(game.get("name"), str)
                or (not game["name"])
            ):
                raise incomplete_error("Invalid category metadata")
        hints_clock = now or datetime_type.now(timezone_type.utc)
        if include_release_hints:
            metadata_games = [
                game
                for game in games
                if (
                    str(game["id"]) not in non_game_ids
                    and str(game.get("igdb_id") or "").isdigit()
                    and str(game["igdb_id"]) not in requested_igdb_ids
                )
            ]
            if metadata_games:
                page_hints, page_hints_status = release_hints(
                    client, metadata_games, hints_clock, deadline=deadline, monotonic=monotonic
                )
                hints.update(page_hints)
                requested_igdb_ids.update((str(game["igdb_id"]) for game in metadata_games))
                hints_statuses.append(page_hints_status)
        page_measured, page_qualified, page_new_measured = (0, 0, 0)
        page_seen = set()
        for game in games:
            game_id = str(game.get("id") or "")
            if game_id in page_seen:
                continue
            page_seen.add(game_id)
            previously_seen = game_id in seen_ids
            seen_ids.add(game_id)
            # Ranking duplicates share one census across every admission source.
            if previously_seen:
                continue
            verification = verification_for(
                game_id, observations, now or datetime_type.now(timezone_type.utc)
            )
            hint = hints.get(str(game.get("igdb_id")), {})
            prediction = evaluate_date(
                hint.get("first_release_date"),
                hint.get("checked_at"),
                hints_clock,
                source="igdb_first_release_date",
            )
            tracked_entry = refresh_tracking(game, prediction, hints_clock)
            already_tracking = tracked_entry is not None and tracked_entry["status"] == "active"
            if game_id not in non_game_ids:
                igdb_predictions[game_id] = prediction
            if game_id in non_game_ids or (
                verification["status"] == "not_new" and (not already_tracking)
            ):
                candidates_by_id.pop(game_id, None)
                channels_by_game.pop(game_id, None)
                below_ids.discard(game_id)
                excluded_by_id[game_id] = {
                    "game_id": game_id,
                    "game_name": game["name"],
                    "igdb_id": game.get("igdb_id") or None,
                    "reason": (
                        "non_game_category" if game_id in non_game_ids else "observed_not_new"
                    ),
                    "verification": verification,
                    "metrics_collected": False,
                    "viewer_threshold_met": None,
                }
                continue
            if not already_tracking and (
                prediction["status"] == "evaluated"
                and prediction["predicted_new"] is False
                or tracked_entry is not None
                and tracked_entry["status"] != "active"
            ):
                candidates_by_id.pop(game_id, None)
                channels_by_game.pop(game_id, None)
                below_ids.discard(game_id)
                excluded_by_id[game_id] = {
                    "game_id": game_id,
                    "game_name": game["name"],
                    "igdb_id": game.get("igdb_id") or None,
                    "reason": "igdb_release_outside_window",
                    "verification": verification,
                    "metrics_collected": False,
                    "viewer_threshold_met": None,
                    "exclusion_source": "igdb_first_release_date",
                    "exclusion_window_days": 30,
                    "release_evidence": hint,
                }
                continue
            excluded_by_id.pop(game_id, None)
            metrics = category_metrics(
                reader, game_id, max_stream_pages, retain_channels=include_filtered_audience
            )
            channels = metrics.pop("_channels", None)
            measured_count += 1
            measured_ids.add(game_id)
            page_measured += 1
            page_new_measured += int(not previously_seen)
            row = {
                "game_id": game_id,
                "game_name": game["name"],
                "box_art_url": game.get("box_art_url"),
                "igdb_id": game.get("igdb_id") or None,
                "verification": verification,
                **metrics,
            }
            if already_tracking:
                retained_by_id[game_id] = row
                if channels is not None:
                    channels_by_game[game_id] = channels
            if metrics["viewer_count"] < min_viewers:
                below_count += 1
                below_ids.add(game_id)
                candidates_by_id.pop(game_id, None)
                if not already_tracking:
                    channels_by_game.pop(game_id, None)
                continue
            page_qualified += 1
            below_ids.discard(game_id)
            if (
                verification["status"] == "not_new"
                or prediction["status"] == "evaluated"
                and prediction["predicted_new"] is False
            ):
                candidates_by_id.pop(game_id, None)
                continue
            candidates_by_id[game_id] = row
            if channels is not None:
                channels_by_game[game_id] = channels
        logger.info(
            "Category page %d: %d scanned, %d measured, %d qualifying",
            page_index + 1,
            len(games),
            page_measured,
            page_qualified,
        )
        if not cursor:
            stop_reason = "category_directory_exhausted"
            break
        # Excluded-only and duplicate-only pages cannot establish the boundary.
        if page_new_measured and (not page_qualified):
            stop_reason = "measured_eligible_page_below_threshold"
            break
        seen_cursors.add(cursor)
        after = cursor
    else:
        raise incomplete_error("Category page limit reached before threshold boundary")
    # Active enrollment receives a complete census even outside the top directory.
    missing = [
        entry
        for game_id, entry in tracking["games"].items()
        if entry["status"] == "active" and game_id not in retained_by_id
    ]
    restored_metadata = {}
    for offset in range(0, len(missing), 100):
        ids = [entry["game_id"] for entry in missing[offset : offset + 100]]
        metadata, _ = reader.get("games", {"id": ids})
        for game in metadata:
            game_id = str(game.get("id") or "")
            if game_id not in ids or not isinstance(game.get("name"), str) or (not game["name"]):
                raise incomplete_error("Invalid tracked category metadata")
            restored_metadata[game_id] = game
    missing_games = [
        {
            "id": entry["game_id"],
            "name": entry["game_name"],
            "igdb_id": entry.get("igdb_id"),
            "box_art_url": entry.get("box_art_url"),
            **restored_metadata.get(entry["game_id"], {}),
        }
        for entry in missing
    ]
    metadata_games = [
        game
        for game in missing_games
        if str(game.get("igdb_id") or "").isdigit()
        and str(game["igdb_id"]) not in requested_igdb_ids
    ]
    hints_clock = now or datetime_type.now(timezone_type.utc)
    if include_release_hints and metadata_games:
        extra_hints, status = release_hints(
            client, metadata_games, hints_clock, deadline=deadline, monotonic=monotonic
        )
        hints.update(extra_hints)
        hints_statuses.append(status)
        requested_igdb_ids.update((str(game["igdb_id"]) for game in metadata_games))
    for game in missing_games:
        game_id = str(game["id"])
        hint = hints.get(str(game.get("igdb_id")), {})
        prediction = evaluate_date(
            hint.get("first_release_date"),
            hint.get("checked_at"),
            hints_clock,
            source="igdb_first_release_date",
        )
        igdb_predictions[game_id] = prediction
        entry = refresh_tracking(game, prediction, hints_clock)
        if entry["status"] != "active":
            continue
        metrics = category_metrics(
            reader, game_id, max_stream_pages, retain_channels=include_filtered_audience
        )
        channels = metrics.pop("_channels", None)
        measured_count += 1
        measured_ids.add(game_id)
        retained_by_id[game_id] = {
            "game_id": game_id,
            "game_name": game["name"],
            "igdb_id": game.get("igdb_id") or None,
            "box_art_url": game.get("box_art_url"),
            "verification": verification_for(game_id, observations, hints_clock),
            **metrics,
        }
        if channels is not None:
            channels_by_game[game_id] = channels
    candidates, excluded = (list(candidates_by_id.values()), list(excluded_by_id.values()))
    measured_rows = {**retained_by_id, **candidates_by_id}
    hints_status = (
        "disabled"
        if not include_release_hints
        else (
            "collection_deadline_exhausted"
            if "collection_deadline_exhausted" in hints_statuses
            else (
                "unavailable_or_partial"
                if "unavailable_or_partial" in hints_statuses
                else "ok" if hints_statuses else "no_igdb_ids"
            )
        )
    )
    for row in list(measured_rows.values()) + excluded:
        hint = hints.get(str(row["igdb_id"]))
        if hint:
            row["release_evidence"] = hint
    candidates.sort(key=lambda row: (-row["viewer_count"], row["game_id"]))
    priority = {"recent_release": 0, "upcoming": 1, "unknown": 2, "older_release": 3}
    queue = [r for r in candidates if r["verification"]["status"] == "pending"]
    queue.sort(
        key=lambda row: (
            priority[row.get("release_evidence", {}).get("release_band", "unknown")],
            -row["viewer_count"],
        )
    )
    finished = now or datetime_type.now(timezone_type.utc)
    experiment = attach_experiments(
        candidates, excluded, hints, release_dates, finished, igdb_predictions=igdb_predictions
    )
    retained_only = [
        row for game_id, row in retained_by_id.items() if game_id not in candidates_by_id
    ]
    attach_experiments(
        retained_only, [], hints, release_dates, finished, igdb_predictions=igdb_predictions
    )
    follower_coverage = None
    if include_filtered_audience:
        resolver = follower_resolver(
            client,
            followers_cache_path,
            max_calls=followers_max_calls,
            max_seconds=followers_max_seconds,
            collection_deadline=deadline,
            monotonic=monotonic,
            utcnow=(lambda: now) if now else None,
        )
        follower_coverage = attach_filtered_audience(
            list(measured_rows.values()), channels_by_game, resolver
        )
        finished = now or datetime_type.now(timezone_type.utc)
    for row in measured_rows.values():
        if steam_matches_by_id is not None:
            row["steam_matches"] = steam_matches_by_id.get(row["game_id"], [])
        # Keep the pre-census eligibility decision at the 30-day boundary.
        prediction = igdb_predictions.get(row["game_id"], {})
        decision_at = prediction.get("evaluated_at")
        enroll_observation(
            tracking,
            row,
            finished,
            min_viewers=min_viewers,
            non_game_ids=non_game_ids,
            eligibility_at=parse_timestamp(decision_at) if decision_at else clock,
            allow_twitch_enrollment=not (
                prediction.get("status") == "evaluated" and prediction.get("predicted_new") is False
            ),
        )
    tracking["updated_at"] = timestamp(finished)
    tracked = [
        row
        for row in measured_rows.values()
        if tracking["games"].get(row["game_id"], {}).get("status") == "active"
    ]
    tracked.sort(key=lambda row: (-row["viewer_count"], row["game_id"]))
    # Reverse lookup enriches intake after enrollment; it never writes the catalog.
    discoveries = None
    if steam_catalog is not None or steam_discovery_state is not None:
        refresh_discoveries = discovery_callback()
        discoveries = refresh_discoveries(
            client,
            tracking,
            steam_catalog,
            steam_discovery_state,
            now or datetime_type.now(timezone_type.utc),
            deadline=deadline,
            monotonic=monotonic,
        )
        finished = now or datetime_type.now(timezone_type.utc)
        tracking["updated_at"] = timestamp(finished)
    return {
        "schema_version": 2,
        "generated_at": timestamp(finished),
        "collection_started_at": timestamp(clock),
        "source": "Twitch Helix API",
        "min_viewers": min_viewers,
        "coverage": {
            "collection_complete": True,
            "stop_reason": stop_reason,
            "max_collection_seconds": max_collection_seconds,
            "collection_elapsed_seconds": round(monotonic() - collection_started, 3),
            "filtered_audience_complete": (
                follower_coverage["partial_categories"] == 0 if follower_coverage else None
            ),
            "category_pages": page_index + 1,
            "categories_seen": len(seen_ids),
            "categories_measured": len(measured_ids),
            "category_measurements": measured_count,
            "duplicate_category_remeasurements": measured_count - len(measured_ids),
            "below_threshold_measurements": below_count,
            "below_threshold_count": len(below_ids),
            "excluded_before_metrics_count": len(excluded),
            "excluded_by_igdb_date_count": sum(
                (r["reason"] == "igdb_release_outside_window" for r in excluded)
            ),
            "igdb_categories_evaluated": sum(
                (p["status"] == "evaluated" for p in igdb_predictions.values())
            ),
            "igdb_categories_unknown": sum(
                (p["status"] == "unknown" for p in igdb_predictions.values())
            ),
            "helix_calls_excluding_retries": reader.calls
            + (follower_coverage["follower_lookup_calls"] if follower_coverage else 0),
            "census_helix_calls_excluding_retries": reader.calls,
            **({"filtered_audience": follower_coverage} if follower_coverage else {}),
            "global_metrics_are_sampled": False,
            "is_simultaneous_global_snapshot": False,
            "all_categories_enumerated": stop_reason == "category_directory_exhausted",
            "discovery_note": (
                "Uses Helix category ranking and stops when a page has newly measured eligible "
                "categories but none measured reaches the threshold. Excluded categories have "
                "unmeasured viewer totals; excluded-only and duplicate-only pages continue. Live "
                "rankings and streams can change during pagination; this is not proof of a "
                "simultaneous, exhaustive global census."
            ),
            "region_note": "Broadcast language is not broadcaster location; no Taiwan/Asia inference is applied.",
            "igdb_hints_status": hints_status,
            "tracked_active_count": len(tracked),
            "tracked_outside_discovery_count": sum(
                (row["game_id"] not in candidates_by_id for row in tracked)
            ),
            "tracking_note": (
                "Twitch new-game discovery and Steam releases within 30 days independently admit "
                "categories. Active source memberships are unioned by Twitch ID and measured "
                "once; directory absence, viewer decline and badge disappearance do not remove "
                "active memberships."
            ),
        },
        "candidate_games": candidates,
        "top_games": [r for r in candidates if r["verification"]["status"] == "new"],
        "pending_verification": [r["game_id"] for r in queue],
        "excluded_games": excluded,
        "newness_experiment": experiment,
        "tracked_games": tracked,
        "tracking_state": tracking,
        "steam_mapping_state": mappings,
        "steam_catalog_summary": steam_summary,
        **(
            (
                {
                    "steam_discovery_state": discoveries,
                    "steam_discovery_summary": discoveries.get("report", {}),
                }
                if discoveries is not None
                else {}
            )
        ),
    }
