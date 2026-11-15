"""Chinese language collection and bounded discovery of new categories."""

from collections import Counter

from requests import RequestException

from radar_backend.domain.twitch_audience import (
    CHINESE_DISCOVERY_MIN_VIEWERS, CHINESE_LANGUAGE_SOURCE, chinese_language, chinese_metrics,
)

SCAN_ERRORS = (RuntimeError, RequestException)
MAX_NEW_CATEGORIES = 12
GAME_IDS_PER_REQUEST = 100
LANGUAGES = ("zh", "zh-hk")


def scan_streams(reader, params, max_pages, *, game_ids=()):
    seen_cursors, found = set(), {}
    try:
        for _ in range(max_pages):
            streams, cursor = reader.get("streams", params)
            for stream in streams:
                game_id, user_id, viewers = (stream.get(key) for key in ("game_id", "user_id", "viewer_count"))
                valid = (isinstance(game_id, str) and game_id.isdigit() and user_id
                         and (not game_ids or game_id in game_ids)
                         and stream.get("type") == "live" and type(viewers) is int and viewers >= 0
                         and chinese_language(stream.get("language")))
                if valid:
                    found[str(user_id)] = stream
                elif game_ids:
                    return {}, "invalid_stream"
            if not cursor:
                return found, "complete"
            if cursor in seen_cursors:
                return {}, "repeated_cursor"
            seen_cursors.add(cursor)
            params = {**params, "after": cursor}
    except SCAN_ERRORS:
        return {}, "unavailable"
    return found, "page_limit"


def scan_languages(reader, languages, max_pages):
    channels, coverage = {}, {}
    for language in languages:
        found, status = scan_streams(reader, {"language": language, "first": 100, "type": "live"}, max_pages)
        coverage[language] = status
        if status in {"complete", "page_limit"}:
            channels.update(found)
    return channels, coverage


def scan_codes(censuses):
    counts = Counter()
    for channels in censuses.values():
        counts.update(code for stream in channels
                      if (code := chinese_language(stream.get("language"))) and code not in LANGUAGES)
    # zh-hk has appeared in the live census. Probe it independently so an API
    # rejection of a regional code cannot discard the ordinary zh results.
    return [*LANGUAGES, *(code for code, _ in counts.most_common(2))]


def discovery_shortlist(channels, excluded_ids):
    totals = Counter()
    for stream in channels.values():
        if stream["game_id"] not in excluded_ids:
            totals[stream["game_id"]] += stream["viewer_count"]
    return [game_id for game_id, viewers in totals.most_common(MAX_NEW_CATEGORIES)
            if viewers >= CHINESE_DISCOVERY_MIN_VIEWERS]


def collect_chinese_audience(reader, rows, max_pages, *, stamp):
    """Attach complete language-filtered results to the selected categories."""
    game_ids = list(rows)
    channels_by_game = {}
    failures = Counter()
    for row in rows.values():
        row.pop("audience_regions", None)
    for offset in range(0, len(game_ids), GAME_IDS_PER_REQUEST):
        batch = game_ids[offset:offset + GAME_IDS_PER_REQUEST]
        regional_codes = {code for game_id in batch for value in rows[game_id].get("language_streamers", {})
                          if (code := chinese_language(value)) and code not in LANGUAGES}
        params = {"game_id": batch, "language": [*LANGUAGES, *sorted(regional_codes)],
                  "first": 100, "type": "live"}
        started = stamp()
        found, status = scan_streams(reader, params, max_pages, game_ids=set(batch))
        finished = stamp()
        if status != "complete":
            failures[status] += len(batch)
            continue
        grouped = {game_id: [] for game_id in batch}
        for stream in found.values():
            grouped[stream["game_id"]].append(stream)
        for game_id, channels in grouped.items():
            rows[game_id]["audience_regions"] = {"zh": {
                **chinese_metrics([stream["viewer_count"] for stream in channels]),
                "measurement_source": CHINESE_LANGUAGE_SOURCE,
                "measurement_started_at": started,
                "measurement_finished_at": finished,
            }}
        channels_by_game.update(grouped)
    return channels_by_game, {
        "requested_categories": len(game_ids),
        "complete_categories": len(channels_by_game),
        "missing_categories_by_reason": dict(failures),
        "helix_calls_excluding_retries": reader.calls,
    }
