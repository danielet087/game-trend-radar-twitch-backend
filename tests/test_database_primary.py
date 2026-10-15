"""Primary database reads fail closed and retain observation-time merge rules."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest
from radar_core.storage import primary
from radar_core.storage.sql import StorageError
from radar_backend.adapters import database_inputs
from radar_backend.state import twitch_followers
from scripts import collection_guard, load_twitch_tracking


def test_input_bundle_uses_one_snapshot_and_never_fabricates_git_commit(monkeypatch):
    monkeypatch.setenv("RADAR_DATABASE_PRIMARY", "1")
    head = {"revision": "db:7", "manifest_digest": "b" * 64, "generation": 7}
    payloads = {
        database_inputs.TRACKING: {"schema_version": 1, "updated_at": "2026-10-15T09:00:00Z", "games": {}},
        database_inputs.CATALOG: {"version": 2, "generated_at": "2026-10-15T09:00:00Z", "count": 0, "games": []},
        database_inputs.MAPPING: {"schema_version": 1, "updated_at": "2026-10-15T09:00:00Z", "games": {}},
        database_inputs.DISCOVERY: {"schema_version": 1, "updated_at": "2026-10-15T09:00:00Z", "games": {}},
    }
    calls = []
    def read(source, paths):
        calls.append((source, paths))
        return head, {p: json.dumps(v).encode() for p, v in payloads.items()}
    monkeypatch.setattr(primary, "read_files", read)
    monkeypatch.setattr(load_twitch_tracking, "frontend_head", lambda: pytest.fail("Git is not primary"))
    result = load_twitch_tracking.load_published_inputs()
    assert len(calls) == 1
    assert result["source_commit"] is None
    assert "_source_commit" not in result["steam_catalog"]
    assert result["source_snapshot"]["manifest_digest"] == "b" * 64
    assert result["steam_catalog"]["_source_snapshot"]["generation"] == 7


def test_database_input_errors_cannot_fallback_to_git_or_empty_state(monkeypatch):
    monkeypatch.setenv("RADAR_DATABASE_PRIMARY", "1")
    monkeypatch.setattr(load_twitch_tracking, "frontend_head", lambda: pytest.fail("Git fallback"))
    def unavailable(*args, **kwargs):
        raise StorageError("AUTHORIZATION_FAILED")
    monkeypatch.setattr(primary, "read_files", unavailable)
    with pytest.raises(StorageError, match="Storage operation"):
        load_twitch_tracking.load_published_inputs()
    monkeypatch.setattr(primary, "read_files", lambda *a, **kw: ({"generation": 1}, {}))
    with pytest.raises(StorageError) as failure: load_twitch_tracking.load_published_inputs()
    assert failure.value.code == "INVALID_TWITCH_DATABASE_INPUT"


def test_guard_reads_database_receipt_even_when_git_projection_is_stale(monkeypatch):
    monkeypatch.setenv("RADAR_DATABASE_PRIMARY", "1")
    receipt = {"schema_version": 1, "collection_complete": True}
    calls = []
    monkeypatch.setattr(primary, "read_json", lambda source, path: calls.append((source, path)) or receipt)
    monkeypatch.setattr(collection_guard.subprocess, "run", lambda *a, **kw: pytest.fail("Git read"))
    assert collection_guard.load_published() == (receipt, None)
    assert calls == [(database_inputs.SOURCE, "data/twitch_collection_status.json")]


def test_follower_cache_primary_error_propagates_and_local_cache_is_not_used(tmp_path, monkeypatch):
    monkeypatch.setenv("RADAR_DATABASE_PRIMARY", "1")
    local = tmp_path / "cache.json"
    local.write_text('{"schema_version":1,"followers":{"1":{"total":9999}}}')
    def unavailable(*args, **kwargs): raise StorageError("AUTHORIZATION_FAILED")
    monkeypatch.setattr(primary, "read_files", unavailable)
    with pytest.raises(StorageError): twitch_followers.load(local, json_module=json)
    monkeypatch.setattr(primary, "read_files", lambda *a, **kw: ({"generation": 1}, {}))
    assert twitch_followers.load(local, json_module=json) == {"schema_version": 1, "followers": {}}
    monkeypatch.setattr(primary, "read_files", lambda *a, **kw: ({"generation": 1}, {twitch_followers.DATABASE_PATH: b"{}"}))
    with pytest.raises(StorageError): twitch_followers.load(local, json_module=json)


def test_cache_cas_retry_preserves_newer_concurrent_counts_and_other_channels(monkeypatch):
    monkeypatch.setenv("RADAR_DATABASE_PRIMARY", "1")
    now = datetime.now(timezone.utc)
    def row(count, age): return {"total": count, "observed_at": (now - timedelta(minutes=age)).isoformat()}
    current = {"1": row(2200, 1), "2": row(3200, 1), "expired": row(3, 1500)}
    reads, writes = [], []
    def read(*args, **kwargs):
        reads.append(1)
        return {"generation": len(reads)}, {twitch_followers.DATABASE_PATH: json.dumps({"schema_version": 1, "followers": current}).encode()}
    def write(source, files, **kwargs):
        writes.append((source, json.loads(files[twitch_followers.DATABASE_PATH]), kwargs))
        if len(writes) == 1: raise StorageError("STALE_GENERATION")
        return {"status": "committed"}
    monkeypatch.setattr(primary, "read_files", read)
    monkeypatch.setattr(primary, "write_files", write)
    assert twitch_followers._database_save({"1": row(2000, 5), "3": row(4000, 1)}, json)
    followers = writes[-1][1]["followers"]
    assert followers["1"]["total"] == 2200
    assert followers["2"]["total"] == 3200
    assert followers["3"]["total"] == 4000
    assert "expired" not in followers
    assert writes[0][2]["operation_id"] == writes[1][2]["operation_id"]
    assert writes[-1][2]["base_generation"] == 2
    assert writes[-1][2]["projection_required"] is False


def test_cache_conflicts_are_bounded_and_database_failure_is_not_a_cache_warning(monkeypatch):
    monkeypatch.setenv("RADAR_DATABASE_PRIMARY", "1")
    monkeypatch.setattr(primary, "read_files", lambda *a, **kw: ({"generation": 1}, {}))
    calls = []
    def conflict(*a, **kw): calls.append(1); raise StorageError("STALE_GENERATION")
    monkeypatch.setattr(primary, "write_files", conflict)
    with pytest.raises(StorageError): twitch_followers._database_save({}, json)
    assert len(calls) == 3


def test_production_workflows_gate_cutover_and_pass_database_env_only_to_runtime():
    root = Path(__file__).resolve().parents[1]
    for name in ("collect.yml", "collect-nintendo.yml"):
        text = (root / ".github/workflows" / name).read_text()
        assert "if: vars.RADAR_DATABASE_MIGRATION_PAUSE != '1'" in text
        assert "RADAR_DATABASE_PRIMARY: ${{ vars.RADAR_DATABASE_PRIMARY || '0' }}" in text
        assert "TURSO_DATABASE_URL" in text and "TURSO_AUTH_TOKEN" in text
    assert "python -m scripts.load_state --output input/nintendo_master.json" in (root / ".github/workflows/collect-nintendo.yml").read_text()


def test_cache_round_trip_uses_real_sqlite_primary_and_has_no_git_delivery(tmp_path, monkeypatch):
    from radar_core.storage import documents, primary_store
    from radar_core.storage.sql import SqliteClient

    monkeypatch.setenv("RADAR_DATABASE_PRIMARY", "1")
    db = SqliteClient(tmp_path / "cache.sqlite")
    try:
        documents.initialize(db)
        primary_store.bootstrap_primary(db, twitch_followers.SOURCE,
            {"data/twitch_release_dates.json": b'{"schema_version":1,"records":{}}'}, "a" * 40)
        monkeypatch.setattr(primary, "client", lambda: db)
        assert twitch_followers.load(tmp_path / "does-not-exist", json_module=json)["followers"] == {}
        row = {"total": 3000, "observed_at": datetime.now(timezone.utc).isoformat()}
        assert twitch_followers._database_save({"123": row}, json)
        restored = twitch_followers.load(tmp_path / "does-not-exist", json_module=json)
        assert restored["followers"] == {"123": row}
        assert primary_store.projection_pending(db, twitch_followers.SOURCE) == []
        head, files = primary_store.read_files(db, twitch_followers.SOURCE)
        assert "data/twitch_release_dates.json" in files
        assert head["generation"] == 2
    finally:
        db.close()
