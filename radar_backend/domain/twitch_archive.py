"""Archive membership comes from recorded admission, never raw popularity."""
from __future__ import annotations

from datetime import datetime, timedelta
import math
import re

from radar_backend.domain.twitch_candidates import NON_GAME_IDS
from radar_backend.domain.twitch_tracking import TRACKING_DAYS

TWITCH_EVIDENCE = {"igdb_first_release_date", "twitch_directory_dom", "twitch_original_release_date"}


def _time(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else None
    except (AttributeError, ValueError, TypeError):
        return None


def qualified_archive_sources(game_id, entry):
    """Keep expired admissions; reject excluded, unverified and untracked rows.

    These are the admission receipts written by the canonical tracking rules.
    Do not reapply today's date or viewer count to an earlier valid admission.
    """
    if (not re.fullmatch(r"[1-9]\d{0,15}", game_id) or game_id in NON_GAME_IDS
            or not isinstance(entry, dict) or entry.get("game_id") != game_id
            or entry.get("status") not in {"active", "expired"}):
        return []
    sources = entry.get("tracking_sources")
    if sources is None:
        sources = {"twitch_new": entry}  # Recorded single-source registry.
    if not isinstance(sources, dict):
        return []
    qualified = []
    for key, source in sources.items():
        if not isinstance(source, dict) or source.get("status") not in {"active", "expired"}:
            continue
        evidence = source.get("enrollment")
        if not isinstance(evidence, dict):
            continue
        observed = _time(evidence.get("observed_at"))
        if not observed:
            continue
        if key == "twitch_new" and evidence.get("source") in TWITCH_EVIDENCE:
            viewers, minimum = evidence.get("viewer_count"), evidence.get("min_viewers")
            if (type(viewers) in {int, float} and type(minimum) in {int, float}
                    and math.isfinite(viewers) and math.isfinite(minimum) and viewers >= minimum >= 7000):
                qualified.append(key)
        elif re.fullmatch(r"steam:[1-9]\d*", key) and evidence.get("source") == "steam_recent_release":
            appid, released = key[6:], _time(evidence.get("release_at"))
            if (str(source.get("steam_appid")) == appid == str(evidence.get("steam_appid"))
                    and released and released <= observed < released + timedelta(days=TRACKING_DAYS)):
                qualified.append(key)
    return sorted(qualified)
