from copy import deepcopy
import json

import pytest

from collectors.twitch_audience import attach_filtered_audience
from collectors.twitch_candidates import category_metrics, PageReader
from radar_backend.state.twitch_series import project_series
from scripts.store_twitch_snapshot import store_snapshot
from tests.test_twitch_audience import resolver
from tests.test_twitch_candidates import FakeClient, page, stream
from tests.test_twitch_tracking_storage import tracked_snapshot


def test_regional_census_uses_latest_deduplicated_broadcast_language():
    client = FakeClient([
        ('streams', page([stream(1, 1, 500), {**stream(1, 2, 1000), 'language': 'en'}], 'next')),
        ('streams', page([{**stream(1, 1, 600), 'language': 'en'}, stream(1, 3, 0),
                          {**stream(1, 4, 200), 'language': 'ZH-tw'},
                          {**stream(1, 5, 50), 'language': '', 'title': '中文直播'}])),
    ])
    result = category_metrics(PageReader(client, 10), '1')
    assert (result['viewer_count'], result['streamer_count']) == (1850, 5)
    assert result['audience_regions']['zh'] == {
        'viewer_count': 200, 'streamer_count': 2, 'median_viewer_count': 100,
    }
    assert '"user_id"' not in json.dumps(result)


def test_unknown_foreign_followers_do_not_hide_complete_chinese_median(tmp_path):
    lookup, network = resolver(tmp_path, [2000], max_calls=1)
    channels = [stream(1, 1, 100), {**stream(1, 2, 50), 'language': 'en'}, stream(1, 3, 9)]
    row = {'game_id': '1', 'viewer_count': 159}
    attach_filtered_audience([row], {'1': channels}, lookup)
    assert row['filtered_audience']['status'] == 'partial'
    chinese = row['audience_regions']['zh']
    assert (chinese['viewer_count'], chinese['streamer_count']) == (109, 2)
    assert chinese['filtered_audience']['status'] == 'complete'
    assert chinese['filtered_audience']['median_viewer_count'] == 100
    assert len(network.requests) == 1


def test_no_chinese_stream_is_observed_zero_with_no_invented_median(tmp_path):
    lookup, _ = resolver(tmp_path, [2000])
    row = {'game_id': '1', 'viewer_count': 100}
    attach_filtered_audience([row], {'1': [{**stream(1, 1, 100), 'language': 'en'}]}, lookup)
    chinese = row['audience_regions']['zh']
    assert chinese['viewer_count'] == chinese['streamer_count'] == 0
    assert chinese['filtered_audience']['status'] == 'complete'
    assert chinese['filtered_audience']['median_viewer_count'] is None


def regional_snapshot():
    payload = tracked_snapshot(100, 2, 50)
    payload['tracked_games'][0]['audience_regions'] = {
        'zh': {'viewer_count': 40, 'streamer_count': 1, 'median_viewer_count': 40}}
    return payload


def test_regions_survive_daily_and_monthly_projection_and_archive_summary(tmp_path):
    payload = regional_snapshot()
    daily = store_snapshot(payload, tmp_path)
    project_series(tmp_path, daily)
    raw = json.loads((tmp_path / daily).read_text())['hours']['2026-09-28T16:00:00Z']['games'][0]
    chunk = json.loads((tmp_path / 'data/twitch_series/1/2026-09.json').read_text())
    assert chunk['hours']['2026-09-28T16:00:00Z']['audience_regions'] == raw['audience_regions']
    game = json.loads((tmp_path / 'data/twitch_series/index.json').read_text())['games']['1']
    assert game['peak_viewer_count'] == 100
    assert game['audience_regions']['zh']['peak_viewer_count'] == 40
    assert game['audience_regions']['zh']['sample_count'] == 1
    before = chunk
    project_series(tmp_path, daily)
    assert json.loads((tmp_path / 'data/twitch_series/1/2026-09.json').read_text()) == before


@pytest.mark.parametrize('change', [
    {'viewer_count': 101}, {'streamer_count': 3}, {'viewer_count': True},
    {'streamer_count': 0}, {'median_viewer_count': float('nan')},
])
def test_invalid_regional_census_cannot_replace_saved_snapshot(tmp_path, change):
    payload = regional_snapshot()
    store_snapshot(payload, tmp_path)
    before = (tmp_path / 'data/twitch_live.json').read_bytes()
    payload['tracked_games'][0]['audience_regions']['zh'].update(change)
    with pytest.raises(ValueError, match='Chinese audience'):
        store_snapshot(payload, tmp_path)
    assert (tmp_path / 'data/twitch_live.json').read_bytes() == before


def test_candidate_and_tracked_regions_must_agree(tmp_path):
    payload = regional_snapshot()
    payload['candidate_games'] = deepcopy(payload['tracked_games'])
    payload['candidate_games'][0]['audience_regions']['zh']['viewer_count'] = 30
    with pytest.raises(ValueError, match='disagree'):
        store_snapshot(payload, tmp_path)
