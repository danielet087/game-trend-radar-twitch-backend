"""Validate durable input documents before opening a collection client."""
from radar_backend.domain.twitch_tracking import normalize_tracking_state
from radar_backend.domain.time import parse_time


def validate_persisted_tracking(payload: dict) -> dict:
    # None is useful for an isolated collector's first run, never a valid
    # downloaded document. The persisted clock is needed for race-safe merges.
    if not isinstance(payload, dict):
        raise ValueError("Persisted tracking state must be an object")
    parse_time(payload.get("updated_at"))
    return normalize_tracking_state(payload)



def validate_persisted_mapping(payload: dict) -> dict:
    from collectors.steam_twitch_mapping import normalize_mapping_state

    if not isinstance(payload, dict) or "updated_at" not in payload:
        raise ValueError("Persisted Steam/Twitch mapping must be a dated object")
    state = normalize_mapping_state(payload)
    if state["games"]:
        parse_time(state.get("updated_at"))
        for entry in state["games"].values():
            if entry.get("status") != "pending" or entry.get("checked_at") is not None:
                parse_time(entry.get("checked_at"))
    return state



def validate_persisted_discovery(payload: dict) -> dict:
    from collectors.twitch_steam_discovery import normalize_discovery_state

    if not isinstance(payload, dict) or "updated_at" not in payload:
        raise ValueError("Persisted Twitch/Steam discovery must be a dated object")
    state = normalize_discovery_state(payload)
    if state["games"]:
        parse_time(state.get("updated_at"))
        for entry in state["games"].values():
            parse_time(entry.get("first_seen_at"))
            parse_time(entry.get("updated_at"))
            if entry.get("checked_at") is not None:
                parse_time(entry["checked_at"])
    return state
