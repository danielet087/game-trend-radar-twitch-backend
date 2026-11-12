"""Bounded Chinese stream discovery and cross-checks against category censuses."""

from collections import Counter

from requests import RequestException

from radar_backend.domain.twitch_audience import CHINESE_DISCOVERY_MIN_VIEWERS, chinese_language

SCAN_ERRORS = (RuntimeError, RequestException)
MAX_NEW_CATEGORIES = 12


def scan_languages(reader, languages, max_pages, *, game_ids=()):
    channels, coverage = {}, {}
    for language in languages:
        params = {"language": language, "first": 100, "type": "live"}
        if game_ids:
            params["game_id"] = list(game_ids)
        seen_cursors, found = set(), {}
        status = "page_limit"
        try:
            for _ in range(max_pages):
                streams, cursor = reader.get("streams", params)
                for stream in streams:
                    game_id, user_id, viewers = (stream.get(key) for key in ("game_id", "user_id", "viewer_count"))
                    if (isinstance(game_id, str) and game_id.isdigit() and user_id
                            and (not game_ids or game_id in game_ids)
                            and stream.get("type") == "live" and type(viewers) is int and viewers >= 0
                            and chinese_language(stream.get("language"))):
                        found[user_id] = stream
                if not cursor:
                    status = "complete"
                    break
                if cursor in seen_cursors:
                    status = "repeated_cursor"
                    break
                seen_cursors.add(cursor)
                params = {**params, "after": cursor}
        except SCAN_ERRORS:
            status = "unavailable"
        coverage[language] = status
        if status in {"complete", "page_limit"}:
            channels.update(found)
    return channels, coverage


def scan_codes(censuses):
    counts = Counter()
    for channels in censuses.values():
        counts.update(code for stream in channels
                      if (code := chinese_language(stream.get("language"))) and code not in {"zh", "zh-hk"})
    # zh-hk has appeared in the live census. Probe it independently so an API
    # rejection of a regional code cannot discard the ordinary zh results.
    return ["zh", "zh-hk", *(code for code, _ in counts.most_common(2))]


def discovery_shortlist(channels, excluded_ids):
    totals = Counter()
    for stream in channels.values():
        if stream["game_id"] not in excluded_ids:
            totals[stream["game_id"]] += stream["viewer_count"]
    return [game_id for game_id, viewers in totals.most_common(MAX_NEW_CATEGORIES)
            if viewers >= CHINESE_DISCOVERY_MIN_VIEWERS]


def audit_censuses(reader, censuses, languages):
    def priority(game_id):
        channels = censuses[game_id]
        has_variant = any(chinese_language(row.get("language")) not in {None, "zh"} for row in channels)
        return (has_variant, len(channels))

    game_ids = sorted(censuses, key=priority, reverse=True)[:3]
    if not game_ids:
        return {"languages": {}, "games": []}
    found, coverage = scan_languages(reader, languages, 3, game_ids=game_ids)
    if not any(status == "complete" for status in coverage.values()):
        return {"languages": coverage, "games": []}
    games = []
    for game_id in game_ids:
        census = {row["user_id"]: chinese_language(row.get("language"))
                  for row in censuses[game_id] if chinese_language(row.get("language"))}
        filtered = {user_id for user_id, row in found.items() if row["game_id"] == game_id}
        only_census = census.keys() - filtered
        games.append({
            "game_id": game_id,
            "census_channels": len(census),
            "language_scan_channels": len(filtered),
            "only_language_scan": len(filtered - census.keys()),
            "only_category_census_by_language": dict(Counter(census[user_id] for user_id in only_census)),
        })
    return {"languages": coverage, "games": games}
