"""Load one immutable frontend revision through explicit input ports."""

from __future__ import annotations


def load_published_tracking(
    *, frontend_head, read_published_json, validate_persisted_tracking, tracking_path
) -> dict:
    """Compatibility helper for registry-only diagnostics."""
    return validate_persisted_tracking(read_published_json(frontend_head(), tracking_path))


def load_published_inputs(
    *,
    frontend_head,
    read_published_json,
    normalize_steam_catalog,
    clock,
    validate_persisted_tracking,
    validate_persisted_mapping,
    validate_persisted_discovery,
    http_error,
    tracking_path,
    steam_path,
    mapping_path,
    discovery_path,
) -> dict:
    """Resolve one frontend revision, then load its complete collection inputs.

    Only absent mapping/discovery files are supported bootstrap conditions. Missing
    required data, invalid JSON and any non-404 HTTP error stop collection.
    """
    commit = frontend_head()
    tracking = validate_persisted_tracking(read_published_json(commit, tracking_path))
    catalog = read_published_json(commit, steam_path)
    normalize_steam_catalog(catalog, clock())
    catalog = {**catalog, "_source_commit": commit}
    try:
        mapping = read_published_json(commit, mapping_path)
    except http_error as error:
        if error.code != 404:
            raise
        mapping = {"schema_version": 1, "updated_at": None, "games": {}}
    mapping = validate_persisted_mapping(mapping)
    try:
        discovery = read_published_json(commit, discovery_path)
    except http_error as error:
        if error.code != 404:
            raise
        discovery = {"schema_version": 1, "updated_at": None, "games": {}}
    discovery = validate_persisted_discovery(discovery)
    return {
        "source_commit": commit,
        "tracking_state": tracking,
        "steam_catalog": catalog,
        "steam_mapping_state": mapping,
        "steam_discovery_state": discovery,
    }
