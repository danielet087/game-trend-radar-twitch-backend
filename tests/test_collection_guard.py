from datetime import datetime, timezone
import io
import json
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from scripts import collection_guard as guard

NOW = datetime(2026, 9, 30, 0, 35, tzinfo=timezone.utc)
SLOT = "2026-09-30T00:00:00Z"


def receipt(**changes):
    return {
        "schema_version": 1, "observed_slot": SLOT, "target_slot": SLOT,
        "collection_started_at": "2026-09-30T00:05:00Z",
        "completed_at": "2026-09-30T00:30:00Z", "generated_at": "2026-09-30T00:30:00Z",
        "collection_complete": True, **changes,
    }


def test_duplicate_after_waiting_for_concurrency_is_skipped():
    decision = guard.decide(now=NOW, receipt=receipt(), requested_slot=SLOT, source="cloudflare")
    assert decision["should_collect"] is False
    assert decision["reason"] == "already_published"


def test_empty_first_run_and_old_data_collect_current_slot():
    assert guard.decide(now=NOW)["should_collect"] is True
    old = receipt(observed_slot="2026-09-29T23:00:00Z", target_slot="2026-09-29T23:00:00Z",
                  collection_started_at="2026-09-29T23:55:00Z")
    # Finishing in this hour does not count as sampling this hour.
    assert guard.decide(now=NOW, receipt=old)["should_collect"] is True


def test_legacy_snapshot_uses_actual_start_not_generated_hour():
    legacy = {"schema_version": 2, "coverage": {"collection_complete": True},
              "collection_started_at": "2026-09-29T23:52:00Z", "generated_at": "2026-09-30T00:17:00Z"}
    assert guard.decide(now=NOW, latest=legacy)["should_collect"] is True
    legacy["collection_started_at"] = "2026-09-30T00:02:00Z"
    assert guard.decide(now=NOW, latest=legacy)["should_collect"] is False


@pytest.mark.parametrize("target", ["2026-09-29T23:00:00Z", "2026-09-30T01:00:00Z"])
def test_stale_or_future_dispatch_does_not_backfill_live_data(target):
    result = guard.decide(now=NOW, requested_slot=target, source="cloudflare")
    assert result["should_collect"] is False
    assert result["reason"] == "stale_or_future_slot"


@pytest.mark.parametrize("target", ["2026-09-30T00:01:00Z", "2026-09-30T00:00:00", "2026-09-30T08:00:00+08:00", "bad\nshould_collect=true"])
def test_invalid_requested_slot_is_rejected(target):
    with pytest.raises(ValueError):
        guard.decide(now=NOW, requested_slot=target)


def test_force_is_explicit_manual_only():
    assert guard.decide(now=NOW, receipt=receipt(), force=True)["should_collect"] is True
    for source in ("cloudflare", "schedule"):
        with pytest.raises(ValueError):
            guard.decide(now=NOW, receipt=receipt(), requested_slot=SLOT, source=source, force=True)
    with pytest.raises(ValueError):
        guard.decide(now=NOW, source="cloudflare")


@pytest.mark.parametrize("changes", [
    {"schema_version": 0}, {"collection_complete": False},
    {"observed_slot": "2026-09-29T23:00:00Z"},
    {"target_slot": "2026-09-30T01:00:00Z"},
    {"collection_started_at": "2026-09-30T00:31:00Z"},
    {"completed_at": "2026-09-30T00:36:00Z", "generated_at": "2026-09-30T00:36:00Z"},
    {"completed_at": "2026-09-30T00:30:00"},
    {"generated_at": "2026-09-30T00:29:00Z"},
])
def test_invalid_receipt_fails_closed(changes):
    with pytest.raises(ValueError):
        guard.decide(now=NOW, receipt=receipt(**changes))


def test_reader_pins_latest_frontend_sha_and_never_sends_tokens(monkeypatch):
    sha = "a" * 40
    monkeypatch.setattr(guard.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=f"{sha}\trefs/heads/main\n"))
    urls = []

    def fetch(request, **kwargs):
        urls.append(request.full_url)
        assert not request.has_header("Authorization")
        assert kwargs["timeout"] == 20
        return io.BytesIO(json.dumps(receipt()).encode())

    monkeypatch.setattr(guard, "urlopen", fetch)
    assert guard.load_published() == (receipt(), None)
    assert urls == [f"https://raw.githubusercontent.com/{guard.FRONTEND}/{sha}/data/twitch_collection_status.json"]


@pytest.mark.parametrize("status", [403, 429, 500])
def test_status_lookup_errors_are_not_treated_as_no_data(monkeypatch, status):
    monkeypatch.setattr(guard.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=f"{'a' * 40}\trefs/heads/main"))

    def fail(*args, **kwargs):
        raise HTTPError("url", status, "error", {}, None)

    monkeypatch.setattr(guard, "urlopen", fail)
    with pytest.raises(HTTPError):
        guard.load_published()


def test_missing_receipt_uses_existing_legacy_snapshot(monkeypatch):
    monkeypatch.setattr(guard.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=f"{'a' * 40}\trefs/heads/main"))

    def fetch(request, **kwargs):
        if request.full_url.endswith("twitch_collection_status.json"):
            raise HTTPError("url", 404, "missing", {}, None)
        return io.BytesIO(b'{"schema_version": 2}')

    monkeypatch.setattr(guard, "urlopen", fetch)
    assert guard.load_published() == (None, {"schema_version": 2})
