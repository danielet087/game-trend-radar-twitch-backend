"""The hourly consumer keeps accepted missing-group Steam observations unknown."""

from copy import deepcopy
import io
import json

import pytest

from collectors.steam_twitch_mapping import normalize_steam_catalog, refresh_mappings
from collectors.twitch_tracking import normalize_tracking_state, reconcile_steam_catalog
from radar_backend.adapters.steam_twitch_mapping import normalize_steam_catalog as normalize_canonical_catalog
from radar_backend.domain.collection import CollectionRequest
from radar_backend.state.json_inputs import JsonCollectionInputStore
from scripts import load_twitch_tracking as loader
from scripts.store_twitch_snapshot import merge_mapping_state, merge_tracking_state
from tests.test_steam_twitch_pipeline import set_frontend_head
from tests.test_twitch_steam_metadata_authority import (
    APPID, CHECKED, NOW, TWITCH_ID, NoNetwork, authoritative_game, catalog,
    discoveries, tracking,
)


def unknown_game():
    row = authoritative_game()
    row.update(
        followers=None, follower_checked_at=None, follower_source=None,
        official_ge5000=False, follower_status="unavailable_group_id",
        follower_unavailable_at=CHECKED, group_id64=None,
        release_start="2026-10-01", release_end="2026-10-01",
        release_store_date="2026-10-01", release_time_utc="2026-10-01T04:02:14Z",
        release_timestamp_taipei_date="2026-10-01", release_date_conflict=False,
        release_date_normalization="steam_store_date_matches_taipei",
    )
    return row


@pytest.mark.parametrize("normalize", [
    normalize_steam_catalog,
    pytest.param(normalize_canonical_catalog, id="canonical-workflow-input"),
])
def test_unknown_catalog_preserves_evidence_without_zero_or_source_mutation(normalize):
    source = catalog(unknown_game())
    before = deepcopy(source)
    row = normalize(source, NOW)[0]
    assert source == before
    assert row["followers"] is None
    assert row["follower_checked_at"] is None and row["follower_source"] is None
    assert row["official_ge5000"] is False
    assert row["follower_status"] == "unavailable_group_id"
    assert row["follower_unavailable_at"] == CHECKED
    assert row["is_recent"] is True
    assert row["twitch_admission"] == source["games"][0]["twitch_admission"]
    row["twitch_admission"]["source_enrollment"]["viewer_count"] = 0
    assert source == before


def test_cached_mapping_and_tracking_survive_unknown_followers_and_publish_merge():
    public = catalog(unknown_game())
    registry = tracking()
    mapping = refresh_mappings(
        NoNetwork(), public, now=NOW, discovery_state=discoveries(),
        tracking_state=registry, allow_lookup=False,
    )
    assert mapping["games"][APPID]["status"] == "matched"
    assert mapping["games"][APPID]["steam"]["followers"] is None
    normalized = normalize_steam_catalog(public, NOW)
    state = normalize_tracking_state(registry, NOW)
    reconcile_steam_catalog(state, normalized, mapping, NOW)
    entry = state["games"][TWITCH_ID]
    source = entry["tracking_sources"]["steam:" + APPID]
    assert source["status"] == "active" and source["steam"]["followers"] is None
    assert source["steam"]["follower_status"] == "unavailable_group_id"
    assert entry["tracking_sources"]["twitch_new"]["enrollment"]["viewer_count"] == 9000
    assert merge_mapping_state(None, mapping)["games"][APPID]["steam"]["followers"] is None
    merged = merge_tracking_state(None, state)
    assert merged["games"][TWITCH_ID]["tracking_sources"]["steam:" + APPID]["steam"]["followers"] is None


def test_hourly_immutable_frontend_loader_accepts_explicit_unknown(monkeypatch):
    sha = set_frontend_head(monkeypatch)
    public = catalog(unknown_game())
    empty = {"schema_version": 1, "updated_at": None, "games": {}}
    sources = {
        loader.TRACKING_PATH: tracking(), loader.STEAM_PATH: public,
        loader.MAPPING_PATH: empty, loader.DISCOVERY_PATH: empty,
    }
    urls = []

    def fetch(request, **kwargs):
        urls.append(request.full_url)
        path = request.full_url.split(f"/{sha}/", 1)[1]
        return io.BytesIO(json.dumps(sources[path]).encode())

    monkeypatch.setattr(loader, "urlopen", fetch)
    result = loader.load_published_inputs()
    assert len(urls) == 4 and all(f"/{sha}/" in url for url in urls)
    assert result["source_commit"] == sha
    assert result["steam_catalog"]["games"][0]["followers"] is None
    assert result["steam_catalog"]["games"][0]["follower_status"] == "unavailable_group_id"


def test_local_collection_input_port_accepts_the_same_unknown_document(tmp_path):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(catalog(unknown_game())))
    request = CollectionRequest(
        output="unused.json", min_viewers=7000, max_category_pages=20,
        max_stream_pages=100, max_api_calls=2000, max_collection_seconds=1500,
        verification_registry="unused.json", release_dates="unused.json",
        no_release_hints=True, followers_cache="unused.json", tracking_state=None,
        steam_catalog=str(path), steam_mapping=None, steam_discovery=None,
        followers_max_calls=None, followers_max_seconds=None, no_filtered_audience=True,
        target_slot=None, trigger_source="manual",
    )
    inputs = JsonCollectionInputStore(clock=lambda: NOW).load(request)
    assert inputs.steam_catalog["games"][0]["followers"] is None


@pytest.mark.parametrize("changes", [
    {"follower_status": None}, {"follower_status": "timeout"},
    {"follower_unavailable_at": None}, {"follower_unavailable_at": "2026-10-02T00:00:00Z"},
    {"follower_checked_at": CHECKED}, {"follower_source": "Steam XML"},
    {"official_ge5000": True}, {"group_id64": "103582791429521412"},
    {"twitch_admission": None}, {"steam_type": "dlc"},
    {"sexual_content_screened": False},
])
def test_null_without_complete_new_core_proof_still_fails_closed(changes):
    row = unknown_game()
    row.update(changes)
    with pytest.raises(ValueError, match="Steam catalog display metadata"):
        normalize_steam_catalog(catalog(row), NOW)


@pytest.mark.parametrize("followers", [-1, True, 7000.0, "7000"])
def test_invalid_numeric_followers_are_not_weakened_by_nullable_support(followers):
    row = unknown_game()
    row["followers"] = followers
    with pytest.raises(ValueError, match="Steam catalog display metadata"):
        normalize_steam_catalog(catalog(row), NOW)
