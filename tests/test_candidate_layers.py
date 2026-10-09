"""Candidate owners preserve census completeness, clocks and metadata failures."""

from collections import Counter
from datetime import datetime, timedelta, timezone
import importlib
import json
from statistics import median

import pytest

from radar_backend.domain import twitch_candidates as rules
from radar_backend.domain.twitch_newness import parse_timestamp, timestamp

NOW = datetime(2026, 9, 28, 17, tzinfo=timezone.utc)


def observation(**changes):
    return {
        "status": "new",
        "source": "twitch_directory_dom",
        "source_url": "https://www.twitch.tv/directory",
        "observed_at": "2026-09-28T16:00:00Z",
        "expires_at": "2026-09-29T16:00:00Z",
        **changes,
    }


def stream(user="1", viewers=7000, **changes):
    return {
        "game_id": "1",
        "user_id": user,
        "viewer_count": viewers,
        "type": "live",
        "language": "zh",
        **changes,
    }


@pytest.mark.parametrize("version", [1, True, 1.0])
def test_registry_preserves_existing_schema_equality(version):
    rows = {"1": observation()}
    assert (
        rules.validate_verifications(
            {"schema_version": version, "observations": rows},
            parse_timestamp=parse_timestamp,
            timedelta_type=timedelta,
        )
        is rows
    )


@pytest.mark.parametrize(
    "change,error",
    [
        ({"status": "unknown"}, ValueError),
        ({"source": "IGDB"}, ValueError),
        ({"source_url": ""}, ValueError),
        ({"expires_at": "2026-09-29T16:00:01Z"}, ValueError),
        ({"expires_at": "2026-09-28T16:00:00Z"}, ValueError),
        ({"observed_at": "bad"}, ValueError),
    ],
)
def test_registry_requires_explicit_short_lived_directory_observation(change, error):
    with pytest.raises(error):
        rules.validate_verifications(
            {"schema_version": 1, "observations": {"1": observation(**change)}},
            parse_timestamp=parse_timestamp,
            timedelta_type=timedelta,
        )


@pytest.mark.parametrize(
    "at,status",
    [
        (NOW - timedelta(hours=1, microseconds=1), "pending"),
        (NOW - timedelta(hours=1), "new"),
        (NOW + timedelta(hours=23, microseconds=-1), "new"),
        (NOW + timedelta(hours=23), "pending"),
    ],
)
def test_verification_uses_half_open_observation_window(at, status):
    row = observation()
    result = rules.verification_for("1", {"1": row}, at, parse_timestamp=parse_timestamp)
    assert result["status"] == status
    assert result is not row


def test_pure_census_retains_last_broadcaster_row_and_zero_viewers():
    channels = {}
    first, latest, zero = stream(viewers=9000), stream(viewers=8100), stream("2", 0)
    assert (
        rules.accumulate_streams(
            channels, [first, latest, zero], "1", incomplete_error=rules.IncompleteCollection
        )
        == 1
    )
    assert channels["1"] is latest
    assert rules.census_metrics(channels, counter_type=Counter, median_fn=median) == {
        "viewer_count": 8100,
        "streamer_count": 2,
        "median_viewer_count": 4050.0,
        "language_streamers": {"zh": 2},
    }


@pytest.mark.parametrize(
    "change",
    [
        {"game_id": "2"},
        {"viewer_count": True},
        {"viewer_count": -1},
        {"viewer_count": "7000"},
        {"viewer_count": None},
        {"user_id": ""},
        {"type": "vod"},
    ],
)
def test_pure_stream_rules_fail_incomplete_measurements(change):
    with pytest.raises(rules.IncompleteCollection):
        rules.accumulate_streams(
            {}, [stream(**change)], "1", incomplete_error=rules.IncompleteCollection
        )


@pytest.mark.parametrize(
    "module_name", ["collectors.twitch_candidates", "radar_backend.adapters.twitch_candidates"]
)
@pytest.mark.parametrize(
    "response",
    [
        None,
        {},
        {"data": {}},
        {"data": [None]},
        {"data": [], "pagination": []},
        {"data": [], "pagination": {"cursor": 1}},
    ],
)
def test_page_reader_rejects_malformed_pages_after_counting_request(module_name, response):
    owner = importlib.import_module(module_name)

    class Client:
        def get(self, endpoint, *, params):
            return response

    reader = owner.PageReader(Client(), 2)
    assert owner.IncompleteCollection is rules.IncompleteCollection
    with pytest.raises(rules.IncompleteCollection):
        reader.get("games/top", {"first": 100})
    assert reader.calls == 1


@pytest.mark.parametrize(
    "module_name", ["collectors.twitch_candidates", "radar_backend.adapters.twitch_candidates"]
)
def test_page_reader_current_error_ports_and_self_fields(module_name, monkeypatch):
    owner = importlib.import_module(module_name)

    class CensusError(RuntimeError):
        pass

    class DeadlineError(RuntimeError):
        pass

    class Client:
        def get(self, endpoint, *, params):
            raise DeadlineError("private")

    reader = owner.PageReader(Client(), 3, deadline=2, monotonic=lambda: 1)
    monkeypatch.setattr(owner, "IncompleteCollection", CensusError)
    monkeypatch.setattr(owner, "CollectionDeadlineExceeded", DeadlineError)
    with pytest.raises(CensusError, match="collection_deadline_exhausted") as failure:
        reader.get("streams", {})
    assert failure.value.__suppress_context__ is True
    assert reader.calls == 1 and reader.max_calls == 3 and reader.deadline == 2


@pytest.mark.parametrize(
    "module_name", ["collectors.twitch_candidates", "radar_backend.adapters.twitch_candidates"]
)
def test_category_uses_real_start_finish_and_current_clock_callback(module_name, monkeypatch):
    owner = importlib.import_module(module_name)
    calls = []
    moments = iter([NOW, NOW + timedelta(seconds=8)])

    class Clock:
        @staticmethod
        def now(zone):
            calls.append(zone)
            return next(moments)

    class Reader:
        def get(self, endpoint, params):
            assert calls == [timezone.utc]
            return [stream()], None

    monkeypatch.setattr(owner, "datetime", Clock)
    metrics = owner.category_metrics(Reader(), "1", retain_channels=True)
    assert calls == [timezone.utc, timezone.utc]
    assert metrics["measurement_started_at"] == timestamp(NOW)
    assert metrics["measurement_finished_at"] == timestamp(NOW + timedelta(seconds=8))
    assert metrics["_channels"][0]["viewer_count"] == 7000


@pytest.mark.parametrize(
    "payload,expected",
    [
        ([{"id": 1}], "unknown"),
        ([{"id": 1, "first_release_date": True}], "unknown"),
        ([{"id": 1, "first_release_date": NOW.timestamp() + 1}], "upcoming"),
        ([{"id": 1, "first_release_date": NOW.timestamp()}], "recent_release"),
        (
            [{"id": 1, "first_release_date": (NOW - timedelta(days=30)).timestamp()}],
            "older_release",
        ),
    ],
)
def test_igdb_shape_preserves_unknown_boolean_and_exact_date_boundary(payload, expected):
    hints = {}
    rules.apply_release_hint_rows(
        payload,
        NOW,
        hints,
        datetime_type=datetime,
        timezone_type=timezone,
        timedelta_type=timedelta,
        source_rules={"igdb_first_release_date": {"window_days": 30}},
        timestamp=timestamp,
    )
    assert hints["1"]["release_band"] == expected
    assert hints["1"]["confirms_twitch_new_badge"] is False


@pytest.mark.parametrize(
    "module_name", ["collectors.twitch_candidates", "radar_backend.adapters.twitch_candidates"]
)
def test_igdb_transport_retains_partial_rows_and_request_finally_stamp(module_name, caplog):
    owner = importlib.import_module(module_name)

    class Client:
        client_id, access_token, timeout_seconds = "id", "secret", 15
        session = None

        def __init__(self):
            self.session = self

        def _wait(self):
            pass

        def post(self, url, *, data, headers, timeout):
            assert url == owner.IGDB_URL
            assert headers == {"Client-ID": "id", "Authorization": "Bearer secret"}
            assert timeout == 15
            return self

        def raise_for_status(self):
            pass

        def json(self):
            return [{"id": 1, "first_release_date": NOW.timestamp()}, {}]

    client = Client()
    hints, status = owner.release_hints(client, [{"igdb_id": "1"}], NOW, monotonic=lambda: 11)
    assert status == "unavailable_or_partial" and set(hints) == {"1"}
    assert client._last_request_at == 11
    assert "secret" not in caplog.text


@pytest.mark.parametrize(
    "module_name", ["collectors.twitch_candidates", "radar_backend.adapters.twitch_candidates"]
)
def test_collect_passes_current_metrics_registry_hints_and_tracking_ports(
    module_name, tmp_path, monkeypatch
):
    owner = importlib.import_module(module_name)
    registry = tmp_path / "observations.json"
    registry.write_text(json.dumps({"schema_version": 1, "observations": {}}))
    dates = tmp_path / "dates.json"
    dates.write_text(json.dumps({"schema_version": 1, "records": {}}))

    class Client:
        def get(self, endpoint, *, params):
            assert endpoint == "games/top"
            return {"data": [{"id": "1", "name": "Fixture", "igdb_id": "101"}], "pagination": {}}

    seen = []

    def metrics(reader, game_id, max_pages, *, retain_channels):
        seen.append((game_id, max_pages, retain_channels))
        return {
            "viewer_count": 7000,
            "streamer_count": 1,
            "median_viewer_count": 7000,
            "language_streamers": {"zh": 1},
            "measurement_started_at": timestamp(NOW),
            "measurement_finished_at": timestamp(NOW),
            "pagination_complete": True,
            "stream_pages": 1,
            "duplicate_broadcasters_removed": 0,
        }

    monkeypatch.setattr(owner, "category_metrics", metrics)
    monkeypatch.setattr(
        owner, "release_hints", lambda *args, **kwargs: ({}, "unavailable_or_partial")
    )
    result = owner.collect_candidates(
        client_id="id",
        client_secret="secret",
        client=Client(),
        now=NOW,
        registry_path=registry,
        release_dates_path=dates,
        monotonic=lambda: 1,
    )
    assert seen == [("1", 150, False)]
    assert result["candidate_games"][0]["game_id"] == "1"
    assert result["coverage"]["igdb_hints_status"] == "unavailable_or_partial"
    assert result["coverage"]["census_helix_calls_excluding_retries"] == 1
    assert result["tracking_state"]["games"] == {}
