from copy import deepcopy

from radar_backend.domain.twitch_archive import qualified_archive_sources
from tests.test_twitch_tracking_storage import tracking_state


def test_archive_requires_proven_admission_and_keeps_it_after_expiry():
    entry = tracking_state('42', status='expired')['games']['42']
    assert qualified_archive_sources('42', entry) == ['twitch_new']
    assert qualified_archive_sources('32982', None) == []
    assert qualified_archive_sources('42', {**entry, 'status': 'excluded'}) == []
    assert qualified_archive_sources('509658', {**entry, 'game_id': '509658'}) == []
    for change in ({'viewer_count': 6999}, {'viewer_count': True}, {'viewer_count': float('nan')},
                   {'source': 'user_requested_legacy_recovery', 'qualification': 'unverified'}, {'observed_at': None}):
        invalid = deepcopy(entry)
        invalid['enrollment'].update(change)
        assert qualified_archive_sources('42', invalid) == []


def test_steam_admission_uses_its_original_release_window_without_a_viewer_threshold():
    entry = tracking_state('42', status='expired')['games']['42']
    source = {'status': 'expired', 'steam_appid': '100', 'enrollment': {
        'source': 'steam_recent_release', 'steam_appid': '100', 'release_at': '2026-09-01T00:00:00Z', 'observed_at': '2026-09-02T00:00:00Z',
    }}
    entry['tracking_sources'] = {'steam:100': source}
    assert qualified_archive_sources('42', entry) == ['steam:100']
    for observed in ['2026-08-31T23:59:59Z', '2026-10-01T00:00:00Z']:
        source['enrollment']['observed_at'] = observed
        assert qualified_archive_sources('42', entry) == []
    source['enrollment']['observed_at'] = '2026-09-02T00:00:00Z'
    source['status'] = 'excluded'
    assert qualified_archive_sources('42', entry) == []
    # Another valid source is sufficient even after a Steam mapping is excluded.
    entry['tracking_sources']['twitch_new'] = {'status': 'expired', 'enrollment': entry['enrollment']}
    assert qualified_archive_sources('42', entry) == ['twitch_new']
