"""Release experiments and immutable frontend loading through offline ports."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from radar_backend.adapters import frontend_input_http as http
from radar_backend.adapters import frontend_inputs as inputs
from radar_backend.adapters import twitch_newness as newness
from radar_backend.application import frontend_inputs as input_application
from radar_backend.domain import twitch_newness as rules
from radar_backend.state import twitch_newness as release_storage

NOW = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)
AT = "2026-09-29T12:00:00Z"


@pytest.mark.parametrize("source,days", list(zip(rules.SOURCES, (14, 30))))
@pytest.mark.parametrize("seconds", (-1, 0, 1))
@pytest.mark.parametrize("offset", (timezone.utc, timezone(timedelta(hours=8))))
def test_source_windows_are_exact_and_never_confirm_badges(source, days, seconds, offset):
    release = (
        (NOW - timedelta(days=days) + timedelta(seconds=seconds)).astimezone(offset).isoformat()
    )
    result = rules.evaluate_date(release, AT, NOW, source=source)
    assert result["predicted_new"] is (seconds > 0)
    assert result["window_days"] == days
    assert result["confirms_twitch_new_badge"] is False
    assert result["status"] == "evaluated"


@pytest.mark.parametrize("source", rules.SOURCES)
@pytest.mark.parametrize(
    "age_seconds,known", [(-1, False), (0, True), (86399, True), (86400, False)]
)
def test_metadata_is_valid_for_a_half_open_24_hour_window(source, age_seconds, known):
    observed = rules.timestamp(NOW - timedelta(seconds=age_seconds))
    result = rules.evaluate_date("2027-09-29T12:00:00Z", observed, NOW, source=source)
    assert (result["status"] == "evaluated") is known
    if known:
        assert result["release_phase"] == "upcoming"
    else:
        assert result["predicted_new"] is None
        assert result["reason"] == "metadata_expired_or_future"


@pytest.mark.parametrize("value", [None, 5, True, {}, [], "", "2026-09-29", "bad-time"])
def test_timestamp_rejects_invalid_or_naive_values(value):
    with pytest.raises(ValueError):
        rules.parse_timestamp(value)


def test_timestamp_normalizes_offset_and_truncates_instead_of_rounding():
    value = datetime(2026, 9, 29, 20, 1, 2, 999999, tzinfo=timezone(timedelta(hours=8)))
    assert rules.timestamp(value) == "2026-09-29T12:01:02Z"
    assert rules.parse_timestamp(value.isoformat()) == value.astimezone(timezone.utc)


def release_document():
    return {
        "schema_version": 1,
        "records": {
            "10": {
                "original_release_date": "2026-09-28T20:00:00.123+08:00",
                "observed_at": AT,
                "source_field": "originalReleaseDate",
                "source_name": "Declared export",
                "source_url": "https://example.org/dates",
                "headers": {"token": "discard"},
            }
        },
    }


@pytest.mark.parametrize(
    "url",
    [
        "http://example.org/dates",
        "https://u@example.org/dates",
        "https://u:p@example.org/dates",
        "https://example.org/dates?token=x",
        "https://example.org/dates#token",
        "https:///dates",
    ],
)
def test_release_provenance_rejects_private_or_non_https_locations(url):
    document = release_document()
    document["records"]["10"]["source_url"] = url
    with pytest.raises(ValueError, match="Source URL"):
        rules.validate_release_dates(document)


def test_release_storage_sanitizes_declared_fields_without_mutating_source(tmp_path):
    document = release_document()
    before = deepcopy(document)
    path = tmp_path / "dates.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    result = release_storage.load_release_dates(path)
    assert document == before
    assert set(result["10"]) == {
        "original_release_date",
        "observed_at",
        "source_field",
        "source_name",
        "source_url",
    }
    assert result["10"]["original_release_date"] == "2026-09-28T12:00:00Z"
    assert newness.load_release_dates(path) == result
    assert (
        newness.RELEASE_DATES_PATH
        == Path(__file__).resolve().parents[1] / "data/twitch_release_dates.json"
    )


def test_attachment_retains_captured_igdb_decision_and_compares_at_badge_time():
    # Capture at the 30-day boundary with valid metadata, then reuse that decision.
    prediction = rules.evaluate_date(
        "2026-08-30T12:00:00Z",
        "2026-09-29T11:59:59Z",
        NOW - timedelta(seconds=1),
        source=rules.SOURCES[1],
    )
    original_prediction = deepcopy(prediction)
    accepted = [
        {
            "game_id": "10",
            "game_name": "Boundary",
            "verification": {"status": "new", "observed_at": "2026-09-29T11:59:59Z"},
        }
    ]
    verification = deepcopy(accepted[0]["verification"])
    report = rules.attach_experiments(
        accepted, [], {}, {}, NOW, igdb_predictions={"10": prediction}
    )
    assert accepted[0]["release_experiment"][rules.SOURCES[1]] == original_prediction
    assert accepted[0]["release_experiment"][rules.SOURCES[1]] is not prediction
    assert accepted[0]["verification"] == verification
    assert prediction == original_prediction
    assert report["reference_checks"][0]["agrees_with_reference"] is True
    assert report["confirms_twitch_new_badge"] is False


def ports(events, documents):
    def read(commit, path):
        events.append(("read", commit, path))
        result = documents[path]
        if isinstance(result, BaseException):
            raise result
        return result

    def validate(name):
        def check(value):
            events.append(("validate", name))
            if not isinstance(value, dict):
                raise ValueError(name)
            return value

        return check

    def clock():
        events.append(("clock",))
        return NOW

    def normalize(catalog, now):
        events.append(("normalize", now))
        if not isinstance(catalog, dict):
            raise ValueError("catalog")

    return {
        "frontend_head": lambda: events.append(("head",)) or "b" * 40,
        "read_published_json": read,
        "normalize_steam_catalog": normalize,
        "clock": clock,
        "validate_persisted_tracking": validate("tracking"),
        "validate_persisted_mapping": validate("mapping"),
        "validate_persisted_discovery": validate("discovery"),
        "http_error": HTTPError,
        "tracking_path": "tracking",
        "steam_path": "catalog",
        "mapping_path": "mapping",
        "discovery_path": "discovery",
    }


def document_ports():
    return {
        "tracking": {"games": {}},
        "catalog": {"games": []},
        "mapping": {"games": {}},
        "discovery": {"games": {}},
    }


def test_bundle_resolves_once_and_preserves_validation_order_and_source_inputs():
    events = []
    documents = document_ports()
    before = deepcopy(documents)
    result = input_application.load_published_inputs(**ports(events, documents))
    assert events == [
        ("head",),
        ("read", "b" * 40, "tracking"),
        ("validate", "tracking"),
        ("read", "b" * 40, "catalog"),
        ("clock",),
        ("normalize", NOW),
        ("read", "b" * 40, "mapping"),
        ("validate", "mapping"),
        ("read", "b" * 40, "discovery"),
        ("validate", "discovery"),
    ]
    assert documents == before
    assert result["steam_catalog"] == {**documents["catalog"], "_source_commit": "b" * 40}
    assert result["tracking_state"] is documents["tracking"]


@pytest.mark.parametrize("missing", [("mapping",), ("discovery",), ("mapping", "discovery")])
def test_only_optional_404_documents_bootstrap(missing):
    documents = document_ports()
    for path in missing:
        documents[path] = HTTPError("url", 404, "not found", {}, None)
    result = input_application.load_published_inputs(**ports([], documents))
    for path in missing:
        assert result[f"steam_{path}_state"] == {
            "schema_version": 1,
            "updated_at": None,
            "games": {},
        }


@pytest.mark.parametrize("path", ["tracking", "catalog", "mapping", "discovery"])
@pytest.mark.parametrize("status", [403, 429, 500])
def test_http_failures_stop_at_the_original_document(path, status):
    documents = document_ports()
    error = HTTPError("url", status, "failure", {}, None)
    documents[path] = error
    events = []
    with pytest.raises(HTTPError) as caught:
        input_application.load_published_inputs(**ports(events, documents))
    assert caught.value is error
    assert events[-1] == ("read", "b" * 40, path)


@pytest.mark.parametrize("path", ["tracking", "catalog"])
def test_required_404_does_not_bootstrap(path):
    documents = document_ports()
    documents[path] = HTTPError("url", 404, "not found", {}, None)
    with pytest.raises(HTTPError):
        input_application.load_published_inputs(**ports([], documents))


@pytest.mark.parametrize("path", ["tracking", "catalog", "mapping", "discovery"])
def test_corrupt_existing_document_stops_before_later_reads(path):
    documents = document_ports()
    documents[path] = None
    events = []
    with pytest.raises(ValueError):
        input_application.load_published_inputs(**ports(events, documents))
    reads = [event[2] for event in events if event[0] == "read"]
    assert reads[-1] == path
    assert len(reads) == ["tracking", "catalog", "mapping", "discovery"].index(path) + 1


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        "a" * 40,
        "A" * 40 + "\trefs/heads/main",
        "a" * 39 + "\trefs/heads/main",
        "a" * 40 + "\trefs/heads/other",
        "a" * 40 + "\trefs/heads/main\nother",
    ],
)
def test_head_resolution_rejects_inexact_git_output(stdout):
    with pytest.raises(ValueError, match="Cannot resolve current frontend HEAD"):
        http.frontend_head(run=lambda *a, **k: SimpleNamespace(stdout=stdout))


def test_http_adapter_uses_public_immutable_url_and_preserves_deadlines():
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(stdout="a" * 40 + "\trefs/heads/main\n")

    commit = http.frontend_head(frontend="owner/frontend", run=run)
    assert calls == [
        (
            ["git", "ls-remote", "https://github.com/owner/frontend.git", "refs/heads/main"],
            {"check": True, "capture_output": True, "text": True, "timeout": 30},
        )
    ]

    def fetch(request, **kwargs):
        assert (
            request.full_url
            == f"https://raw.githubusercontent.com/owner/frontend/{commit}/data/tracking.json"
        )
        assert request.headers == {"User-agent": "game-trend-radar-tracking-loader"}
        assert kwargs == {"timeout": 20}
        return io.BytesIO(b'{"games": {}}')

    assert http.read_published_json(
        commit, "data/tracking.json", frontend="owner/frontend", open_url=fetch
    ) == {"games": {}}


@pytest.mark.parametrize(
    "module_name", ["collectors.twitch_newness", "radar_backend.adapters.twitch_newness"]
)
def test_newness_facades_resolve_current_callbacks(monkeypatch, module_name):
    import importlib

    facade = importlib.import_module(module_name)
    monkeypatch.setattr(facade, "timestamp", lambda value: "patched-stamp")
    result = facade.evaluate_date(None, None, NOW, source=rules.SOURCES[0])
    assert result["evaluated_at"] == "patched-stamp"


@pytest.mark.parametrize(
    "module_name", ["scripts.load_twitch_tracking", "radar_backend.adapters.frontend_inputs"]
)
def test_input_facades_resolve_current_document_helpers(monkeypatch, module_name):
    import importlib

    facade = importlib.import_module(module_name)
    calls = []
    monkeypatch.setattr(facade, "frontend_head", lambda: "c" * 40)
    monkeypatch.setattr(
        facade,
        "read_published_json",
        lambda commit, path: calls.append((commit, path)) or {"marker": "loaded"},
    )
    monkeypatch.setattr(
        facade, "validate_persisted_tracking", lambda payload: {"validated": payload}
    )
    monkeypatch.setattr(facade, "TRACKING_PATH", "custom/tracking.json")
    assert facade.load_published_tracking() == {"validated": {"marker": "loaded"}}
    assert calls == [("c" * 40, "custom/tracking.json")]
