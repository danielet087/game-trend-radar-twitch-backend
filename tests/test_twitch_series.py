import json
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from radar_backend.state.twitch_series import HEAT_PROJECTION, heat_projection, project_series
from radar_backend.jobs.project_twitch_series import restore_covers
from tests.test_twitch_tracking_storage import tracking_state


def release_mapping(released="2026-09-03", appid="100", **changes):
    entry = {"steam_appid": appid, "status": "matched", "twitch_game_id": "42",
             "steam": {"steam_appid": appid, "release_date": released, "release_precision": "day",
                       "release_date_timezone": "Asia/Taipei"}}
    entry.update(changes)
    return {"games": {appid: entry}}


def project_release_fixture(root, registry, mapping=None):
    history = root / "data/twitch_history/2026-10-01.json"
    history.parent.mkdir(parents=True, exist_ok=True)
    hour = "2026-09-30T16:00:00Z"
    history.write_text(json.dumps({"schema_version": 1, "timezone": "Asia/Taipei", "date": "2026-10-01",
        "hours": {hour: {"generated_at": hour, "games": [{"game_id": "42", "game_name": "Test", "viewer_count": 17}]}}}))
    (root / "data/twitch_tracking.json").write_text(json.dumps(registry))
    path = root / "data/twitch_steam_mapping.json"
    if mapping is not None:
        path.write_text(json.dumps(mapping))
    elif path.exists():
        path.unlink()
    project_series(root)
    return json.loads((root / "data/twitch_series/index.json").read_text())["games"]["42"]


class TwitchSeriesTests(unittest.TestCase):
    def test_release_reference_prefers_matched_steam_taiwan_day_over_timestamp_audit_and_igdb(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            registry = tracking_state("42", status="expired")
            registry["games"]["42"]["release_at"] = "2026-09-04T00:00:00Z"
            mapping = release_mapping()
            mapping["games"]["100"]["steam"].update(
                release_at="2026-09-04T04:02:14Z", release_time_utc="2026-09-04T04:02:14Z",
                release_date_conflict=True, release_store_date="2026-09-03",
                release_time_basis="steam_taiwan_store_date_authoritative")
            before = deepcopy(registry)
            game = project_release_fixture(root, registry, mapping)
            self.assertEqual(game["release_reference"], {"date": "2026-09-03", "source": "Steam 台灣商店",
                "precision": "day", "source_type": "steam_taiwan_store_date", "steam_appid": "100"})
            self.assertEqual(registry, before)
            self.assertEqual(json.loads((root / "data/twitch_tracking.json").read_text()), before)
            self.assertEqual(game["sample_count"], 1)
            self.assertEqual(game["peak_viewer_count"], 17)

    def test_release_reference_rejects_unmatched_wrong_game_or_conflicting_app_identity(self):
        for changes in ({"status": "pending"}, {"status": "not_matched"}, {"twitch_game_id": "43"},
                        {"steam_appid": "101"}, {"steam": {"steam_appid": "101", "release_date": "2026-09-03",
                         "release_precision": "day", "release_date_timezone": "Asia/Taipei"}}):
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as temp:
                registry = tracking_state("42")
                # A previously resolved alias is not a current mapping decision.
                registry["games"]["42"]["steam_matches"] = [{"steam_appid": "100", "release_date": "2026-09-03",
                    "release_precision": "day", "release_date_timezone": "Asia/Taipei"}]
                game = project_release_fixture(Path(temp), registry, release_mapping(**changes))
                self.assertEqual(game["release_reference"]["source"], "IGDB 首次發售日")
                self.assertEqual(game["release_reference"]["date"], "2026-09-20")

    def test_release_reference_requires_real_precise_steam_taiwan_date(self):
        for changes in ({"release_date": "2026-02-30"}, {"release_date": "2026-9-3"}, {"release_date": "Q3 2026"},
                        {"release_precision": "month"}, {"release_date_timezone": "UTC"}, {"release_date_timezone": None}):
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as temp:
                mapping = release_mapping()
                mapping["games"]["100"]["steam"].update(changes)
                game = project_release_fixture(Path(temp), tracking_state("42"), mapping)
                self.assertEqual(game["release_reference"]["source_type"], "igdb_first_release_date")

    def test_release_reference_does_not_choose_between_matched_editions_with_different_dates(self):
        with tempfile.TemporaryDirectory() as temp:
            mapping = release_mapping()
            mapping["games"].update(release_mapping("2026-09-04", "101")["games"])
            game = project_release_fixture(Path(temp), tracking_state("42"), mapping)
            self.assertEqual(game["release_reference"]["source"], "IGDB 首次發售日")
            self.assertNotIn("steam_appid", game["release_reference"])
            mapping["games"]["101"]["steam"]["release_date"] = "2026-09-03"
            game = project_release_fixture(Path(temp), tracking_state("42"), mapping)
            self.assertEqual(game["release_reference"]["date"], "2026-09-03")
            self.assertEqual(game["release_reference"]["source"], "Steam 台灣商店")
            self.assertNotIn("steam_appid", game["release_reference"])

    def test_release_reference_fallback_uses_only_qualified_source_and_taiwan_calendar(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            registry = tracking_state("42")
            entry = registry["games"]["42"]
            entry.update(release_source="twitch_original_release_date", release_at="2026-09-19T17:00:00Z")
            entry["enrollment"]["source"] = "twitch_original_release_date"
            game = project_release_fixture(root, registry)
            self.assertEqual(game["release_reference"], {"date": "2026-09-20", "source": "Twitch 首次發售日",
                "precision": "day", "source_type": "twitch_original_release_date"})
            valid_steam = {"status": "active", "steam_appid": "100", "release_at": "2026-09-20T00:00:00Z",
                "enrollment": {"source": "steam_recent_release", "steam_appid": "100",
                    "observed_at": "2026-09-21T00:00:00Z", "release_at": "2026-09-20T00:00:00Z"}}
            invalid_legacy = deepcopy(entry)
            invalid_legacy["enrollment"]["source"] = "user_requested_legacy_recovery"
            entry["tracking_sources"] = {"twitch_new": invalid_legacy, "steam:100": valid_steam}
            # The legitimate Steam admission retains the archive game, but it
            # cannot validate an unrelated legacy IGDB/Twitch release claim.
            game = project_release_fixture(root, registry)
            self.assertEqual(game["admission_sources"], ["steam:100"])
            self.assertNotIn("release_reference", game)

    def test_release_reference_is_removed_when_reliable_source_disappears(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            registry = tracking_state("42")
            game = project_release_fixture(root, registry, release_mapping())
            self.assertEqual(game["release_reference"]["source"], "Steam 台灣商店")
            chunk = root / "data/twitch_series/42/2026-10.json"
            metrics = chunk.read_bytes()
            raw = (root / "data/twitch_history/2026-10-01.json").read_bytes()
            registry["games"]["42"].pop("release_at")
            game = project_release_fixture(root, registry)
            self.assertNotIn("release_reference", game)
            self.assertEqual(chunk.read_bytes(), metrics)
            self.assertEqual((root / "data/twitch_history/2026-10-01.json").read_bytes(), raw)

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
            (root / 'data/twitch_tracking.json').write_text(json.dumps(tracking_state('42')))
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
            (root / 'data/twitch_tracking.json').write_text(json.dumps(tracking_state('42')))
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
            registry = tracking_state('42', at='2026-10-22T00:00:00Z', status='expired')
            registry['games']['42'].update(box_art_url='https://static-cdn.jtvnw.net/ttv-boxart/42_IGDB-{width}x{height}.jpg',
                steam_matches=[{'steam_appid': '100', 'display_name': '鬼武者', 'name_en': 'Onimusha'}])
            (root / 'data/twitch_tracking.json').write_text(json.dumps(registry))
            project_series(root)
            game = json.loads((root / 'data/twitch_series/index.json').read_text())['games']['42']
            self.assertEqual(game['tracking_status'], 'ended')
            self.assertIs(game['archive_eligible'], True)
            self.assertEqual(game['display_name'], '鬼武者')
            self.assertEqual(game['sample_count'], 2)
            self.assertEqual(game['peak_viewer_count'], 14)
            self.assertEqual(game['box_art_url'], registry['games']['42']['box_art_url'])
            self.assertIn('Onimusha', game['aliases'])
            # A new independent source can resume tracking without losing history.
            registry['games']['42'].update(status='active', tracking_sources={
                'twitch_new': {'status': 'expired', 'enrollment': registry['games']['42']['enrollment']},
                'steam:100': {'status': 'active', 'steam_appid': '100', 'release_at': '2026-10-20T00:00:00Z', 'expires_at': '2026-11-19T00:00:00Z',
                    'enrollment': {'source': 'steam_recent_release', 'steam_appid': '100', 'observed_at': '2026-10-21T00:00:00Z', 'release_at': '2026-10-20T00:00:00Z'}},
            })
            (root / 'data/twitch_tracking.json').write_text(json.dumps(registry))
            project_series(root, 'data/twitch_history/2026-10-01.json')
            game = json.loads((root / 'data/twitch_series/index.json').read_text())['games']['42']
            self.assertEqual(game['tracking_status'], 'active')
            self.assertEqual(game['sample_count'], 2)
            self.assertEqual(game['first_hour'], '2026-09-30T15:00:00Z')
            registry['games']['42'].pop('box_art_url')
            (root / 'data/twitch_tracking.json').write_text(json.dumps(registry))
            project_series(root)
            game = json.loads((root / 'data/twitch_series/index.json').read_text())['games']['42']
            self.assertEqual(game['box_art_url'], 'https://static-cdn.jtvnw.net/ttv-boxart/42_IGDB-{width}x{height}.jpg')

    def test_popular_raw_samples_never_reenter_archive_after_full_or_incremental_rebuild(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / 'data/twitch_history/2026-10-01.json'
            path.parent.mkdir(parents=True)
            hour = '2026-09-30T16:00:00Z'
            path.write_text(json.dumps({'schema_version': 1, 'timezone': 'Asia/Taipei', 'date': '2026-10-01', 'hours': {
                hour: {'generated_at': hour, 'games': [
                    {'game_id': '32982', 'game_name': 'Grand Theft Auto V', 'viewer_count': 274474},
                    {'game_id': '42', 'game_name': 'Onimusha', 'viewer_count': 0},
                ]},
            }}))
            raw = path.read_bytes()
            (root / 'data/twitch_tracking.json').write_text(json.dumps(tracking_state('42', status='expired')))
            index_path = root / 'data/twitch_series/index.json'
            index_path.parent.mkdir(parents=True)
            # Even an older public index claiming membership cannot overrule the registry.
            index_path.write_text(json.dumps({'schema_version': 1, 'timezone': 'Asia/Taipei', 'months': {},
                'games': {'32982': {'name': 'Grand Theft Auto V', 'months': [], 'archive_eligible': True}}}))
            for day in (None, 'data/twitch_history/2026-10-01.json', None):
                project_series(root, day)
                index = json.loads(index_path.read_text())
                self.assertEqual(set(index['games']), {'42'})
                self.assertEqual(index['games']['42']['sample_count'], 1)
                self.assertEqual(index['games']['42']['tracking_status'], 'ended')
                self.assertEqual(path.read_bytes(), raw)
            published = index_path.read_bytes()
            (root / 'data/twitch_tracking.json').unlink()
            with self.assertRaisesRegex(ValueError, 'Tracking registry is required'):
                project_series(root)
            self.assertEqual(index_path.read_bytes(), published)

    def test_heat_files_keep_only_the_planning_fields_for_every_archived_month(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            history = root / 'data/twitch_history'
            history.mkdir(parents=True)
            filtered = {'rule': 'followers_gt_1000_viewers_gte_10_v1', 'status': 'complete', 'eligible_streamer_count': 1,
                        'eligible_viewer_count': 40, 'median_viewer_count': 40}
            chinese = {'viewer_count': 30, 'streamer_count': 3, 'median_viewer_count': 9, 'largest_stream_viewer_count': 20,
                       'measurement_source': 'helix_language_filter', 'measurement_started_at': '2026-09-30T16:01:00Z',
                       'measurement_finished_at': '2026-09-30T16:09:00Z', 'streamer_counts_above_viewers': [1, 0, 0],
                       'filtered_audience': filtered}
            full = {'game_id': '42', 'game_name': 'Test', 'viewer_count': 120, 'streamer_count': 7,
                    'streamer_counts_above_viewers': [2, 1, 0], 'filtered_audience': filtered,
                    'audience_regions': {'zh': chinese, 'ja': {'viewer_count': 5}}}
            days = {'2026-09-30': ('2026-09-30T15:00:00Z', [{'game_id': '42', 'game_name': 'Test', 'viewer_count': 14,
                                                             'audience_regions': {'zh': None}}]),
                    '2026-10-01': ('2026-09-30T16:00:00Z', [full])}
            for date, (hour, games) in days.items():
                (history / f'{date}.json').write_text(json.dumps({'schema_version': 1, 'timezone': 'Asia/Taipei', 'date': date,
                    'hours': {hour: {'generated_at': hour.replace(':00:00Z', ':12:00Z'), 'games': games}}}))
            (root / 'data/twitch_tracking.json').write_text(json.dumps(tracking_state('42')))
            project_series(root)
            base = root / 'data/twitch_series/42'
            october = json.loads((base / '2026-10-heat.json').read_text())
            self.assertEqual(october, {'schema_version': 1, 'timezone': 'Asia/Taipei', 'game_id': '42', 'month': '2026-10',
                'projection': HEAT_PROJECTION, 'hours': {'2026-09-30T16:00:00Z': {
                    'generated_at': '2026-09-30T16:12:00Z', 'viewer_count': 120, 'streamer_count': 7,
                    'audience_regions': {'zh': {'viewer_count': 30, 'streamer_count': 3, 'largest_stream_viewer_count': 20,
                                                'measurement_source': 'helix_language_filter'}}}}})
            self.assertEqual(json.loads((base / '2026-09-heat.json').read_text())['hours'],
                             {'2026-09-30T15:00:00Z': {'generated_at': '2026-09-30T15:12:00Z', 'viewer_count': 14}})
            self.assertEqual(heat_projection(json.loads((base / '2026-10.json').read_text())), october)
            self.assertEqual(json.loads((base / '2026-10.json').read_text())['hours']['2026-09-30T16:00:00Z']['filtered_audience'], filtered)
            # An hourly run also backfills a month it did not touch, and keeps
            # unchanged heat files byte for byte.
            (base / '2026-09-heat.json').unlink()
            before = (base / '2026-10-heat.json').read_bytes()
            project_series(root, 'data/twitch_history/2026-10-01.json')
            self.assertEqual(json.loads((base / '2026-09-heat.json').read_text())['hours'],
                             {'2026-09-30T15:00:00Z': {'generated_at': '2026-09-30T15:12:00Z', 'viewer_count': 14}})
            self.assertEqual((base / '2026-10-heat.json').read_bytes(), before)
            index = json.loads((root / 'data/twitch_series/index.json').read_text())
            self.assertEqual(index['games']['42']['months'], ['2026-09', '2026-10'])
            # A corrected census that omits the game removes its heat row too.
            day = history / '2026-10-01.json'
            corrected = json.loads(day.read_text())
            corrected['hours']['2026-09-30T16:00:00Z']['games'] = []
            day.write_text(json.dumps(corrected))
            project_series(root, 'data/twitch_history/2026-10-01.json')
            self.assertEqual(json.loads((base / '2026-10-heat.json').read_text())['hours'], {})

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


    def test_steam_game_tracked_before_release_is_active_with_its_first_live_hour(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            registry = tracking_state("42", at="2026-10-01T03:20:00Z")
            entry = registry["games"]["42"]
            release = "2026-10-05T16:00:00Z"
            entry.update(status="active", status_reason="steam_pre_release",
                         release_at=release, expires_at="2026-11-04T16:00:00Z")
            entry["tracking_sources"] = {"steam:100": {
                "status": "active", "status_reason": "steam_pre_release", "steam_appid": "100",
                "release_at": release, "expires_at": "2026-11-04T16:00:00Z",
                "enrollment": {"source": "steam_recent_release", "steam_appid": "100",
                               "observed_at": "2026-09-30T16:00:00Z", "release_at": release}}}
            (root / "data").mkdir(parents=True)
            (root / "data/twitch_tracking.json").write_text(json.dumps(registry))
            history = root / "data/twitch_history/2026-10-01.json"
            history.parent.mkdir(parents=True)
            counts = {"2026-09-30T16:00:00Z": (0, 0), "2026-09-30T17:00:00Z": (0, 0),
                      "2026-09-30T18:00:00Z": (2, 130)}
            history.write_text(json.dumps({"schema_version": 1, "timezone": "Asia/Taipei", "date": "2026-10-01",
                "hours": {hour: {"generated_at": hour, "games": [{"game_id": "42", "game_name": "Upcoming",
                    "viewer_count": viewers, "streamer_count": streamers}]}
                    for hour, (streamers, viewers) in counts.items()}}))
            project_series(root)
            game = json.loads((root / "data/twitch_series/index.json").read_text())["games"]["42"]
            self.assertEqual(game["admission_sources"], ["steam:100"])
            self.assertEqual(game["tracking_status"], "active")
            self.assertEqual(game["first_hour"], "2026-09-30T16:00:00Z")
            self.assertEqual(game["first_live_hour"], "2026-09-30T18:00:00Z")
            # More than two weeks before the release, the same receipt admits nothing.
            entry["tracking_sources"]["steam:100"]["enrollment"]["observed_at"] = "2026-09-21T15:59:59Z"
            (root / "data/twitch_tracking.json").write_text(json.dumps(registry))
            project_series(root)
            index = json.loads((root / "data/twitch_series/index.json").read_text())
            self.assertNotIn("42", index["games"])


if __name__ == '__main__':
    unittest.main()
