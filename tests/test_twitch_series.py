import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from radar_backend.state.twitch_series import project_series
from radar_backend.jobs.project_twitch_series import restore_covers


class TwitchSeriesTests(unittest.TestCase):
    def test_backfill_incremental_replacement_and_missing_hours(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            day = root / 'data/twitch_history/2026-10-01.json'
            day.parent.mkdir(parents=True)
            hour = '2026-09-30T16:00:00Z'
            generated = '2026-09-30T16:12:00Z'
            filtered = {'status': 'partial', 'unknown_follower_count': 2}
            sample = {'generated_at': generated, 'games': [
                {'game_id': '42', 'game_name': 'Test', 'viewer_count': 0, 'streamer_count': 0, 'filtered_audience': filtered},
                {'game_id': '43', 'viewer_count': 99, 'observation_status': 'retained'},
                {'game_id': '../unsafe', 'viewer_count': 10},
            ]}
            daily = {'schema_version': 1, 'timezone': 'Asia/Taipei', 'date': '2026-10-01', 'hours': {hour: sample}}
            day.write_text(json.dumps(daily))
            project_series(root)
            base = root / 'data/twitch_series'
            index = json.loads((base / 'index.json').read_text())
            self.assertEqual(index['months'], {'2026-10': {hour: generated}})
            self.assertEqual(set(index['games']), {'42'})
            self.assertEqual(index['games']['42']['sample_count'], 1)
            self.assertEqual(index['games']['42']['peak_viewer_count'], 0)
            chunk = base / '42/2026-10.json'
            rows = json.loads(chunk.read_text())['hours']
            self.assertEqual(rows[hour]['viewer_count'], 0)
            self.assertEqual(rows[hour]['filtered_audience'], filtered)
            # A corrected census omitting 42 must remove its prior metric row,
            # while preserving the fact that this hour was collected.
            sample['games'] = []
            day.write_text(json.dumps(daily))
            project_series(root, str(day.relative_to(root)))
            self.assertEqual(json.loads(chunk.read_text())['hours'], {})
            corrected = json.loads((base / 'index.json').read_text())['games']['42']
            self.assertEqual(corrected['sample_count'], 0)
            self.assertIsNone(corrected['peak_viewer_count'])
            self.assertEqual(json.loads((base / 'index.json').read_text())['months'], index['months'])
            before = (base / 'index.json').read_bytes()
            project_series(root, str(day.relative_to(root)))
            self.assertEqual((base / 'index.json').read_bytes(), before)

    def test_scheduled_cross_hour_completion_uses_collection_start(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / 'data/twitch_history/2026-10-01.json'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'schema_version': 1, 'timezone': 'Asia/Taipei', 'date': '2026-10-01', 'hours': {
                '2026-10-01T15:00:00Z': {'generated_at': '2026-10-01T16:04:00Z',
                    'collection_started_at': '2026-10-01T15:05:00Z', 'collection_schedule': {'target_slot': 'test'},
                    'games': [{'game_id': '42', 'game_name': 'Test', 'viewer_count': 12}]},
                '2026-10-01T14:00:00Z': {'generated_at': '2026-10-01T16:04:00Z', 'games': []},
            }}))
            project_series(root)
            index = json.loads((root / 'data/twitch_series/index.json').read_text())
            self.assertEqual(list(index['months']['2026-10']), ['2026-10-01T15:00:00Z'])

    def test_archive_keeps_ended_games_with_aliases_and_cross_month_totals(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            history = root / 'data/twitch_history'
            history.mkdir(parents=True)
            for date, hour, count in [('2026-09-30', '2026-09-30T15:00:00Z', 14), ('2026-10-01', '2026-09-30T16:00:00Z', 0)]:
                (history / f'{date}.json').write_text(json.dumps({'schema_version': 1, 'timezone': 'Asia/Taipei', 'date': date,
                    'hours': {hour: {'generated_at': hour, 'games': [{'game_id': '42', 'game_name': 'Onimusha', 'viewer_count': count}]}}}))
            registry = {'schema_version': 1, 'updated_at': '2026-10-22T00:00:00Z', 'games': {
                '42': {'status': 'expired', 'box_art_url': 'https://static-cdn.jtvnw.net/ttv-boxart/42_IGDB-{width}x{height}.jpg',
                       'steam_matches': [{'steam_appid': '100', 'display_name': '鬼武者', 'name_en': 'Onimusha'}]},
            }}
            (root / 'data/twitch_tracking.json').write_text(json.dumps(registry))
            project_series(root)
            game = json.loads((root / 'data/twitch_series/index.json').read_text())['games']['42']
            self.assertEqual(game['tracking_status'], 'ended')
            self.assertEqual(game['display_name'], '鬼武者')
            self.assertEqual(game['sample_count'], 2)
            self.assertEqual(game['peak_viewer_count'], 14)
            self.assertEqual(game['box_art_url'], registry['games']['42']['box_art_url'])
            self.assertIn('Onimusha', game['aliases'])
            # A new independent source can resume tracking without losing history.
            registry['games']['42'].update(status='active', tracking_sources={
                'twitch_new': {'status': 'expired'},
                'steam:100': {'status': 'active', 'release_at': '2026-10-20T00:00:00Z', 'expires_at': '2026-11-19T00:00:00Z'},
            })
            (root / 'data/twitch_tracking.json').write_text(json.dumps(registry))
            project_series(root, 'data/twitch_history/2026-10-01.json')
            game = json.loads((root / 'data/twitch_series/index.json').read_text())['games']['42']
            self.assertEqual(game['tracking_status'], 'active')
            self.assertEqual(game['sample_count'], 2)
            self.assertEqual(game['first_hour'], '2026-09-30T15:00:00Z')
            registry['games'].clear()
            (root / 'data/twitch_tracking.json').write_text(json.dumps(registry))
            project_series(root)
            game = json.loads((root / 'data/twitch_series/index.json').read_text())['games']['42']
            self.assertEqual(game['box_art_url'], 'https://static-cdn.jtvnw.net/ttv-boxart/42_IGDB-{width}x{height}.jpg')

    def test_cover_backfill_only_requests_missing_artwork_and_preserves_metrics(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / 'data/twitch_series/index.json'
            path.parent.mkdir(parents=True)
            cover = 'https://static-cdn.jtvnw.net/ttv-boxart/42_IGDB-{width}x{height}.jpg'
            index = {'generated_at': '2026-10-22T00:00:00Z', 'games': {
                '41': {'box_art_url': cover, 'sample_count': 95}, '42': {'box_art_url': None, 'sample_count': 12}, '43': {'box_art_url': None},
            }}
            path.write_text(json.dumps(index))
            requested = []
            def lookup(ids):
                requested.extend(ids)
                return {'42': SimpleNamespace(box_art_url=cover), '43': SimpleNamespace(box_art_url='https://example.com/cover.jpg')}
            self.assertEqual(restore_covers(root, lookup), 1)
            self.assertEqual(requested, ['42', '43'])
            result = json.loads(path.read_text())
            index['games']['42']['box_art_url'] = cover
            self.assertEqual(result, index)


if __name__ == '__main__':
    unittest.main()
