from datetime import datetime, timedelta, timezone
import json

import pytest

from collectors.twitch_newness import attach_experiments, evaluate_date, load_release_dates
from scripts.import_twitch_release_dates import extract_records

NOW = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)
CAPTURED = "2026-09-29T12:00:00Z"


@pytest.mark.parametrize("release_at,prediction,phase", [
    ("2026-09-15T12:00:01Z", True, "released_within_14_days"),
    ("2026-09-15T12:00:00Z", False, "older_release"),
    ("2026-09-15T20:00:00+08:00", False, "older_release"),
    ("2026-09-29T12:00:00Z", True, "released_within_14_days"),
    ("2027-09-29T12:00:00Z", True, "upcoming"),
    ("2021-02-02T00:00:00Z", False, "older_release"),
])
def test_glance_rule_boundary_offsets_and_future_are_explicit(release_at, prediction, phase):
    result = evaluate_date(release_at, CAPTURED, NOW, source="twitch_original_release_date")
    assert result["predicted_new"] is prediction
    assert result["release_phase"] == phase
    assert result["confirms_twitch_new_badge"] is False


@pytest.mark.parametrize("release_at,captured", [
    (None, CAPTURED), ("bad-date", CAPTURED), ("2026-09-28", CAPTURED),
    ("2026-09-28T00:00:00Z", "2026-09-28T12:00:00Z"),
    ("2026-09-28T00:00:00Z", "2026-09-29T12:00:01Z"),
])
def test_missing_invalid_expired_or_future_metadata_is_unknown_not_false(release_at, captured):
    result = evaluate_date(release_at, captured, NOW, source="twitch_original_release_date")
    assert result["status"] == "unknown" and result["predicted_new"] is None


def test_igdb_never_fills_twitch_original_date_or_changes_official_status():
    candidates = [{"game_id": "10", "game_name": "Recent", "igdb_id": "100", "verification": {"status": "pending"}}]
    hints = {"100": {"checked_at": CAPTURED, "first_release_date": "2026-09-28T00:00:00Z"}}
    report = attach_experiments(candidates, [], hints, {}, NOW)
    assert candidates[0]["verification"] == {"status": "pending"}
    assert candidates[0]["release_experiment"]["twitch_original_release_date"]["predicted_new"] is None
    assert candidates[0]["release_experiment"]["igdb_first_release_date"]["predicted_new"] is True
    assert report["twitch_original_release_date"]["predicted_new_game_ids"] == []
    assert report["igdb_first_release_date"]["predicted_new_game_ids"] == ["10"]


def test_badge_comparison_uses_observation_time_and_reports_date_mismatch():
    candidates = [{"game_id": "10", "game_name": "Boundary", "igdb_id": "100",
                   "verification": {"status": "new", "observed_at": "2026-09-28T16:00:00Z"}}]
    # Under 14 days when the badge was seen, but over 14 days now.
    hints = {"100": {"checked_at": CAPTURED, "first_release_date": "2026-09-15T00:00:00Z"}}
    report = attach_experiments(candidates, [], hints, {}, NOW)
    assert candidates[0]["release_experiment"]["igdb_first_release_date"]["predicted_new"] is False
    comparison = report["reference_checks"][0]
    assert comparison["agrees_with_reference"] is True
    assert comparison["comparison_kind"] == "retrospective"
    assert candidates[0]["verification"]["status"] == "new"
    # A conflicting date is retained as disagreement, never used to remove NEW.
    hints["100"]["first_release_date"] = "2021-02-02T00:00:00Z"
    report = attach_experiments(candidates, [], hints, {}, NOW)
    assert report["reference_checks"][0]["agrees_with_reference"] is False
    assert candidates[0]["verification"]["status"] == "new"


def test_saved_response_import_discards_unrelated_fields_and_missing_dates(tmp_path):
    payload = [{"data": {"directoriesWithTags": {"edges": [
        {"node": {"id": "10", "originalReleaseDate": "2026-09-28T00:00:00Z", "unrelated_token": "do-not-copy"}},
        {"node": {"id": "20", "originalReleaseDate": None}},
    ]}}}]
    records = extract_records(payload, source_name="Saved response", source_url="https://www.twitch.tv/directory", observed_at=CAPTURED)
    path = tmp_path / "dates.json"
    path.write_text(json.dumps({"schema_version": 1, "records": records}))
    dates = load_release_dates(path)
    assert list(dates) == ["10"]
    assert "do-not-copy" not in json.dumps(dates)
    candidates = [{"game_id": "10", "game_name": "Test", "verification": {"status": "pending"}}]
    report = attach_experiments(candidates, [], {}, dates, NOW)
    assert report["twitch_original_release_date"]["predicted_new_game_ids"] == ["10"]
    assert report["igdb_first_release_date"]["unknown_candidates"] == 1


def test_dataset_import_uses_capture_time_instead_of_import_time():
    payload = [{"categoryId": "10", "originalReleaseDate": "2026-09-20T00:00:00Z", "scrapedAt": CAPTURED}]
    records = extract_records(payload, source_name="Independent dataset", source_url="https://example.org/dataset")
    assert records["10"]["observed_at"] == CAPTURED
    old_result = evaluate_date(records["10"]["original_release_date"], CAPTURED, NOW + timedelta(days=1), source="twitch_original_release_date")
    assert old_result["predicted_new"] is None


@pytest.mark.parametrize("payload", [
    [{"id": 100, "first_release_date": 1790640000}],
    {"data": {"game": {"id": "10", "originalReleaseDate": "2026-09-28T00:00:00Z"}}},
    {"errors": [{"message": "not authorized"}], "data": {}},
    {"log": {"entries": []}},
])
def test_import_rejects_wrong_source_missing_capture_time_and_error_responses(payload):
    with pytest.raises(ValueError):
        extract_records(payload, source_name="Export", source_url="https://example.org/dataset")


def test_release_registry_rejects_igdb_field_and_credential_url(tmp_path):
    record = {"original_release_date": "2026-09-28T00:00:00Z", "observed_at": CAPTURED,
              "source_name": "Export", "source_url": "https://example.org/dataset", "source_field": "first_release_date"}
    path = tmp_path / "dates.json"
    for changes in ({}, {"source_field": "originalReleaseDate", "source_url": "https://example.org/dataset?token=test"}):
        path.write_text(json.dumps({"schema_version": 1, "records": {"10": {**record, **changes}}}))
        with pytest.raises(ValueError):
            load_release_dates(path)
