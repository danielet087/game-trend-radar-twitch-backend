"""Pure qualified enrollment and official Twitch/IGDB/Steam discovery evidence."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta

from radar_backend.domain.steam_twitch_mapping import _id
from radar_backend.domain.twitch_newness import parse_timestamp, timestamp

_UNSET = object()

METHOD = "twitch_igdb_external_steam_v1"
POLICY_VERSION = 2
TWITCH_IDENTITY_METHOD = "igdb_external_twitch_uid_v1"
RETRY_INTERVAL = timedelta(hours=24)
STATUSES = {"matched", "no_steam_link", "pending", "unavailable"}
ENROLLMENT_SOURCES = {
    "igdb_first_release_date",
    "twitch_original_release_date",
    "twitch_directory_dom",
}
NON_GAME_IDS = {"509658", "509672", "509663", "509659", "26936"}


def normalize_website_identity(value, twitch_id, igdb_id):
    from radar_backend.domain.twitch_steam_website_identity import (
        normalize_website_identity as normalize,
    )

    return normalize(value, twitch_id, igdb_id)


def _ids(value, *, id_fn=_UNSET) -> list[str]:
    id_fn = _id if id_fn is _UNSET else id_fn
    if not isinstance(value, list):
        raise ValueError("Discovery IDs must be a list")
    ids = [id_fn(item) for item in value]
    if len(set(ids)) != len(ids):
        raise ValueError("Discovery IDs must be unique")
    return sorted(ids, key=int)


def _enrollment(
    value,
    clock: datetime | None = None,
    *,
    enrollment_sources=_UNSET,
    parse_timestamp_fn=_UNSET,
    deepcopy_fn=_UNSET,
) -> dict:
    enrollment_sources = (
        ENROLLMENT_SOURCES if enrollment_sources is _UNSET else enrollment_sources
    )
    parse_timestamp_fn = (
        parse_timestamp if parse_timestamp_fn is _UNSET else parse_timestamp_fn
    )
    deepcopy_fn = deepcopy if deepcopy_fn is _UNSET else deepcopy_fn
    if (
        not isinstance(value, dict)
        or value.get("source") not in enrollment_sources
        or type(value.get("min_viewers")) is not int
        or value["min_viewers"] != 7000
        or type(value.get("viewer_count")) is not int
        or value["viewer_count"] < value["min_viewers"]
        or value.get("qualification") == "unverified"
    ):
        raise ValueError("Discovery requires a qualified Twitch enrollment")
    observed = parse_timestamp_fn(value.get("observed_at"))
    if clock is not None and observed > clock:
        raise ValueError("Twitch enrollment cannot be from the future")
    return deepcopy_fn(value)


def _twitch_identity(
    value: dict,
    twitch_id: str,
    igdb_id: str | None,
    *,
    identity_method=_UNSET,
    id_fn=_UNSET,
    parse_timestamp_fn=_UNSET,
    deepcopy_fn=_UNSET,
) -> dict:
    """Validate the exact official Twitch UID fallback, never a name match."""
    identity_method = (
        TWITCH_IDENTITY_METHOD if identity_method is _UNSET else identity_method
    )
    id_fn = _id if id_fn is _UNSET else id_fn
    parse_timestamp_fn = (
        parse_timestamp if parse_timestamp_fn is _UNSET else parse_timestamp_fn
    )
    deepcopy_fn = deepcopy if deepcopy_fn is _UNSET else deepcopy_fn
    if (
        not isinstance(value, dict)
        or value.get("method") != identity_method
        or id_fn(value.get("twitch_game_id")) != twitch_id
        or id_fn(value.get("igdb_id")) != igdb_id
    ):
        raise ValueError("Invalid fallback Twitch identity")
    source = id_fn(value.get("twitch_source_id"))
    parse_timestamp_fn(value.get("checked_at"))
    links = value.get("links")
    if not isinstance(links, list) or not links:
        raise ValueError("Fallback identity requires official Twitch links")
    seen = set()
    for link in links:
        if (
            not isinstance(link, dict)
            or id_fn(link.get("external_game_source")) != source
            or link.get("uid") != twitch_id
            or id_fn(link.get("game")) != igdb_id
        ):
            raise ValueError("Fallback Twitch link does not match")
        external_id = id_fn(link.get("external_game_id"))
        if external_id in seen:
            raise ValueError("Duplicate fallback Twitch external identity")
        seen.add(external_id)
    return deepcopy_fn(value)


def _invalidate_identity(
    row: dict, reason: str, at: str, retry_at: str, *, policy_version=_UNSET
) -> None:
    """A completed contradictory lookup invalidates an old positive decision."""
    policy_version = POLICY_VERSION if policy_version is _UNSET else policy_version
    # A never-identified category has no completed Steam identity to date.
    # Invalidating a previously checked owner does need a newer decision clock
    # so publication races cannot restore its old store proof.
    checked_at = (
        at
        if reason != "missing_twitch_igdb_identity" or row.get("checked_at")
        else None
    )
    row.update(
        status="pending",
        reason=reason,
        igdb_id=None,
        steam_appids=[],
        links=[],
        public_steam_appids=[],
        missing_public_appids=[],
        checked_at=checked_at,
        retry_at=retry_at,
        lookup_policy_version=policy_version,
    )
    for key in (
        "igdb_identity",
        "related_steam_identity",
        "related_lookup_status",
        "related_checked_at",
        "related_retry_at",
    ):
        row.pop(key, None)


def normalize_discovery_state(
    payload: dict | None = None,
    *,
    deepcopy_fn=_UNSET,
    parse_timestamp_fn=_UNSET,
    id_fn=_UNSET,
    ids_fn=_UNSET,
    statuses=_UNSET,
    method=_UNSET,
    enrollment_fn=_UNSET,
    policy_version=_UNSET,
    twitch_identity_fn=_UNSET,
    website_normalizer=_UNSET,
) -> dict:
    """Copy and validate persisted identities and their exact official evidence."""
    deepcopy_fn = deepcopy if deepcopy_fn is _UNSET else deepcopy_fn
    parse_timestamp_fn = (
        parse_timestamp if parse_timestamp_fn is _UNSET else parse_timestamp_fn
    )
    id_fn = _id if id_fn is _UNSET else id_fn
    ids_fn = _ids if ids_fn is _UNSET else ids_fn
    statuses = STATUSES if statuses is _UNSET else statuses
    method = METHOD if method is _UNSET else method
    enrollment_fn = _enrollment if enrollment_fn is _UNSET else enrollment_fn
    policy_version = POLICY_VERSION if policy_version is _UNSET else policy_version
    twitch_identity_fn = (
        _twitch_identity if twitch_identity_fn is _UNSET else twitch_identity_fn
    )
    website_normalizer = (
        normalize_website_identity
        if website_normalizer is _UNSET
        else website_normalizer
    )
    if payload is None:
        return {"schema_version": 1, "updated_at": None, "games": {}}
    if (
        not isinstance(payload, dict)
        or type(payload.get("schema_version")) is not int
        or payload["schema_version"] != 1
        or not isinstance(payload.get("games"), dict)
    ):
        raise ValueError("Invalid Twitch/Steam discovery registry")
    state = deepcopy_fn(payload)
    if state.get("updated_at") is not None:
        parse_timestamp_fn(state["updated_at"])
    source = (
        id_fn(state["steam_source_id"])
        if state.get("steam_source_id") is not None
        else None
    )
    if source is not None:
        state["steam_source_id"] = source
    twitch_source = (
        id_fn(state["twitch_source_id"])
        if state.get("twitch_source_id") is not None
        else None
    )
    if twitch_source is not None:
        state["twitch_source_id"] = twitch_source
    if state.get("source_catalog") is not None:
        catalog = state["source_catalog"]
        if not isinstance(catalog, dict) or type(catalog.get("count")) is not int:
            raise ValueError("Invalid discovery catalog source")
        parse_timestamp_fn(catalog.get("generated_at"))
        catalog["appids"] = ids_fn(catalog.get("appids"))
        if catalog["count"] != len(catalog["appids"]):
            raise ValueError("Discovery catalog source count does not match")
    for twitch_id, row in state["games"].items():
        if (
            not isinstance(twitch_id, str)
            or id_fn(twitch_id) != twitch_id
            or not isinstance(row, dict)
            or id_fn(row.get("twitch_game_id")) != twitch_id
            or row.get("status") not in statuses
            or row.get("method") != method
            or not isinstance(row.get("twitch_name"), str)
            or not row["twitch_name"].strip()
            or type(row.get("active")) is not bool
        ):
            raise ValueError("Invalid discovery identity, status, or provenance")
        enrollment_fn(row.get("twitch_enrollment"))
        parse_timestamp_fn(row.get("first_seen_at"))
        parse_timestamp_fn(row.get("updated_at"))
        for key in ("checked_at", "retry_at"):
            if row.get(key) is not None:
                parse_timestamp_fn(row[key])
        row["igdb_id"] = (
            id_fn(row["igdb_id"]) if row.get("igdb_id") is not None else None
        )
        if "lookup_policy_version" in row and (
            type(row["lookup_policy_version"]) is not int
            or not 1 <= row["lookup_policy_version"] <= policy_version
        ):
            raise ValueError("Invalid discovery lookup policy")
        if row.get("igdb_identity") is not None:
            row["igdb_identity"] = twitch_identity_fn(
                row["igdb_identity"], twitch_id, row["igdb_id"]
            )
            if (
                row["igdb_identity"]["twitch_source_id"] != twitch_source
                or parse_timestamp_fn(row["igdb_identity"]["checked_at"])
                > parse_timestamp_fn(row["updated_at"])
                or state.get("updated_at") is None
                or parse_timestamp_fn(row["updated_at"])
                > parse_timestamp_fn(state["updated_at"])
            ):
                raise ValueError(
                    "Fallback identity source or time does not match its registry"
                )
        if row.get("related_steam_identity") is not None:
            row["related_steam_identity"] = website_normalizer(
                row["related_steam_identity"], twitch_id, row["igdb_id"]
            )
            if (
                row["status"] != "no_steam_link"
                or parse_timestamp_fn(row["related_steam_identity"]["checked_at"])
                > parse_timestamp_fn(row["updated_at"])
                or state.get("updated_at") is None
                or parse_timestamp_fn(row["updated_at"])
                > parse_timestamp_fn(state["updated_at"])
            ):
                raise ValueError(
                    "Website identity does not match its canonical discovery or time"
                )
        if row.get("related_lookup_status") is not None:
            if row["related_lookup_status"] not in {
                "matched",
                "no_steam_website",
                "unavailable",
            }:
                raise ValueError("Invalid related identity lookup status")
            if row["status"] != "no_steam_link" or not row["igdb_id"]:
                raise ValueError("Related lookup requires a canonical IGDB identity")
            if row["related_lookup_status"] == "matched" and not row.get(
                "related_steam_identity"
            ):
                raise ValueError("Related match requires website evidence")
            if row["related_lookup_status"] == "no_steam_website" and row.get(
                "related_steam_identity"
            ):
                raise ValueError(
                    "A negative website decision cannot carry identity evidence"
                )
        for key in ("related_checked_at", "related_retry_at"):
            if row.get(key) is not None:
                parsed = parse_timestamp_fn(row[key])
                if key == "related_checked_at" and parsed > parse_timestamp_fn(
                    row["updated_at"]
                ):
                    raise ValueError("Related identity check cannot be from the future")
        if (
            row.get("related_steam_identity")
            and row.get("related_checked_at") is not None
            and row["related_checked_at"] != row["related_steam_identity"]["checked_at"]
        ):
            raise ValueError("Related decision time must match its verified proof")
        appids = row["steam_appids"] = ids_fn(row.get("steam_appids"))
        if not isinstance(row.get("links"), list):
            raise ValueError("Discovery requires official external links")
        covered, identities = set(), set()
        for link in row["links"]:
            if not isinstance(link, dict):
                raise ValueError("Invalid discovery external link")
            appid = id_fn(link.get("steam_appid"))
            external_id = id_fn(link.get("external_game_id"))
            if (
                not source
                or id_fn(link.get("external_game_source")) != source
                or id_fn(link.get("game")) != row["igdb_id"]
                or link.get("uid") != appid
                or appid not in appids
                or link.get("url") != f"https://store.steampowered.com/app/{appid}/"
                or external_id in identities
            ):
                raise ValueError("Discovery external identity does not match")
            identities.add(external_id)
            covered.add(appid)
        if covered != set(appids):
            raise ValueError("Discovery AppIDs require corresponding external evidence")
        if row["status"] == "matched":
            if not appids or not row["igdb_id"] or not row.get("checked_at"):
                raise ValueError(
                    "Matched discovery requires checked official identities"
                )
        elif appids or row["links"]:
            raise ValueError("Unconfirmed discovery cannot carry confirmed links")
        if row["status"] == "no_steam_link" and (
            not source or not row["igdb_id"] or not row.get("checked_at")
        ):
            raise ValueError("Missing Steam link requires a successful official lookup")
        public = row["public_steam_appids"] = ids_fn(row.get("public_steam_appids", []))
        missing = row["missing_public_appids"] = ids_fn(
            row.get("missing_public_appids", [])
        )
        if set(public) & set(missing) or set(public) | set(missing) != set(appids):
            raise ValueError("Discovery catalog membership does not match its AppIDs")
        if state.get("source_catalog") is not None:
            catalog_ids = set(state["source_catalog"]["appids"])
            if (
                set(public) != set(appids) & catalog_ids
                or set(missing) != set(appids) - catalog_ids
            ):
                raise ValueError(
                    "Discovery catalog membership does not match its source snapshot"
                )
    return state


def _catalog_ids(payload: dict, *, parse_timestamp_fn=_UNSET, id_fn=_UNSET) -> set[str]:
    parse_timestamp_fn = (
        parse_timestamp if parse_timestamp_fn is _UNSET else parse_timestamp_fn
    )
    id_fn = _id if id_fn is _UNSET else id_fn
    if (
        not isinstance(payload, dict)
        or payload.get("version") != 2
        or not isinstance(payload.get("games"), list)
        or type(payload.get("count")) is not int
        or payload["count"] != len(payload["games"])
    ):
        raise ValueError("Invalid public Steam catalog envelope")
    parse_timestamp_fn(payload.get("generated_at"))
    ids = []
    for row in payload["games"]:
        if not isinstance(row, dict):
            raise ValueError("Invalid public Steam catalog game")
        ids.append(id_fn(row.get("appid")))
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate public Steam AppID")
    return set(ids)


def _qualified_members(
    tracking: dict,
    clock: datetime,
    *,
    non_game_ids=_UNSET,
    enrollment_fn=_UNSET,
    id_fn=_UNSET,
) -> dict[str, tuple[dict, dict]]:
    non_game_ids = NON_GAME_IDS if non_game_ids is _UNSET else non_game_ids
    enrollment_fn = _enrollment if enrollment_fn is _UNSET else enrollment_fn
    id_fn = _id if id_fn is _UNSET else id_fn
    result = {}
    for twitch_id, entry in tracking["games"].items():
        source = entry.get("tracking_sources", {}).get("twitch_new")
        if (
            twitch_id in non_game_ids
            or entry.get("status") != "active"
            or not isinstance(source, dict)
            or source.get("source") != "twitch_new"
            or source.get("status") != "active"
        ):
            continue
        try:
            evidence = enrollment_fn(source.get("enrollment"), clock)
        except (ValueError, TypeError):
            # Legacy manual restorations and Steam admissions cannot create
            # a second automatic source or evade the original Twitch gate.
            continue
        result[id_fn(twitch_id)] = (entry, evidence)
    return result
