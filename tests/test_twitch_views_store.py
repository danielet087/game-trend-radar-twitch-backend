"""The database-only Twitch views: mirror and primary publication modes."""
import json

import pytest

from radar_core.publication import SubprocessGitRepository
from radar_core.storage import documents, primary, primary_store
from radar_core.storage.sql import SqliteClient, StorageError
from radar_backend.publication.twitch import merge_frozen_snapshot, publish_snapshot
from radar_backend.state import twitch_views_store as views
from tests.test_standalone import git
from tests.test_twitch_publication import durable_payload, read_remote, repository  # noqa: F401

VIEWS = views.REPOSITORY + "@" + views.BRANCH
SEED = {
    "data/twitch_history/2026-09-28.json": b'{"schema_version": 1, "date": "2026-09-28", "hours": []}',
    "data/twitch_series/index.json": b'{"schema_version": 1}',
}


@pytest.fixture
def database(tmp_path, monkeypatch):
    client = SqliteClient(tmp_path / "radar.sqlite")
    documents.initialize(client)
    primary_store.bootstrap_primary(client, VIEWS, SEED, "seed")
    monkeypatch.setattr(primary, "client", lambda: client)
    monkeypatch.setenv("RADAR_DATABASE_PRIMARY", "1")
    yield client
    client.close()


def stored(client):
    return primary_store.read_files(client, VIEWS)[1]


FLAG_BYTES = views.flag_document(generation=1, files=2, raw_bytes=10, moved_at="2026-11-18T00:00:00Z")


def test_the_flag_file_selects_the_stage_and_a_bad_flag_stops(tmp_path):
    assert views.views_mode(tmp_path) == "mirror"
    (tmp_path / views.FLAG).parent.mkdir(parents=True)
    (tmp_path / views.FLAG).write_bytes(FLAG_BYTES)
    assert views.views_mode(tmp_path) == "primary"
    for bad in (b"not json", b'{"schema_version": 1, "home": "git", "source": "%s"}' % views.SOURCE.encode(),
                b'{"schema_version": 1, "home": "database", "source": "other@branch"}'):
        (tmp_path / views.FLAG).write_bytes(bad)
        with pytest.raises(ValueError):
            views.views_mode(tmp_path)


def test_the_database_flag_is_read_from_the_frontend_source(database):
    frontend = views.REPOSITORY + "@" + views.FRONTEND_BRANCH
    primary_store.bootstrap_primary(database, frontend, {"data/catalog.json": b"{}"}, "frontend")
    assert views.mode_in_database() == "mirror"
    head = primary_store.read_files(database, frontend, paths=())[0]
    primary_store.publish_patch(database, frontend, base_generation=head["generation"],
                                changed={views.FLAG: FLAG_BYTES}, operation_id="flag", projection_required=False)
    assert views.mode_in_database() == "primary"


def test_a_primary_write_never_overwrites_a_newer_generation(database, tmp_path):
    root = tmp_path / "checkout"
    assert views.materialize(root)["generation"] == 1
    (root / "data/twitch_series/index.json").write_bytes(b'{"schema_version": 1, "mine": true}')
    head = primary_store.read_files(database, VIEWS, paths=())[0]
    primary_store.publish_patch(database, VIEWS, base_generation=head["generation"],
                                changed={"data/twitch_history_close/2026-09-27.json": b'{"other": 1}'},
                                operation_id="other-writer", projection_required=False)
    with pytest.raises(StorageError) as stale:
        views.sync(root, payload_revision="e" * 64, base_generation=1)
    assert stale.value.code == "STALE_GENERATION"
    assert stored(database)["data/twitch_series/index.json"] == SEED["data/twitch_series/index.json"]
    assert views.sync(root, payload_revision="e" * 64, base_generation=2)["status"] == "written"


def test_read_into_and_delete_work_on_the_views_source_only(database, tmp_path):
    head = views.read_into(tmp_path, ("data/twitch_history/",))
    assert sorted(views.local_views(tmp_path)) == ["data/twitch_history/2026-09-28.json"]
    assert views.read(())[1] == {} and head["generation"] == 1
    with pytest.raises(ValueError):
        views.delete(["data/twitch_live.json"], base_generation=1, payload_revision="f" * 64)
    with pytest.raises(StorageError):
        views.delete(["data/twitch_history/2026-09-28.json"], base_generation=7, payload_revision="f" * 64)
    result = views.delete(["data/twitch_history/2026-09-28.json"], base_generation=1, payload_revision="f" * 64)
    assert result == {"status": "written", "generation": 2, "deleted": 1}
    assert sorted(stored(database)) == ["data/twitch_series/index.json"]


def test_materialize_refuses_a_source_without_the_series_index(database, tmp_path):
    views.delete(["data/twitch_series/index.json"], base_generation=1, payload_revision="f" * 64)
    with pytest.raises(StorageError) as missing:
        views.materialize(tmp_path)
    assert missing.value.code == "MISSING_VIEWS"


def test_sync_writes_differences_and_deletions_then_is_idempotent(database, tmp_path):
    root = tmp_path / "checkout"
    (root / "data/twitch_history_compact").mkdir(parents=True)
    (root / "data/twitch_history_compact/2026-09-28.json").write_bytes(b'{"games": {}}')
    (root / "data/twitch_series").mkdir(parents=True)
    (root / "data/twitch_series/index.json").write_bytes(SEED["data/twitch_series/index.json"])
    (root / "data/twitch_live.json").write_bytes(b'{"not": "a view"}')
    first = views.sync(root, payload_revision="a" * 64)
    assert first["status"] == "written" and first["changed"] == 1 and first["deleted"] == 1
    assert stored(database) == {"data/twitch_history_compact/2026-09-28.json": b'{"games": {}}',
                                "data/twitch_series/index.json": SEED["data/twitch_series/index.json"]}
    assert views.sync(root, payload_revision="a" * 64)["status"] == "unchanged"


def test_sync_retries_a_concurrent_writer(database, tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    (root / "data/twitch_series").mkdir(parents=True)
    (root / "data/twitch_series/index.json").write_bytes(b'{"schema_version": 1, "new": true}')
    real, calls = primary.write_files, []

    def stale_once(*args, **kwargs):
        calls.append(kwargs["base_generation"])
        if len(calls) == 1:
            raise StorageError("STALE_GENERATION", "moved")
        return real(*args, **kwargs)

    monkeypatch.setattr(primary, "write_files", stale_once)
    assert views.sync(root, payload_revision="b" * 64)["attempts"] == 2


def test_materialize_replaces_local_views_and_leaves_other_files(database, tmp_path):
    root = tmp_path / "checkout"
    (root / "data/twitch_history_close").mkdir(parents=True)
    (root / "data/twitch_history_close/stale.json").write_bytes(b"{}")
    (root / "data/catalog.json").write_bytes(b'{"keep": 1}')
    assert views.materialize(root) == {"generation": 1, "files": 2}
    assert views.local_views(root) == SEED
    assert (root / "data/catalog.json").read_bytes() == b'{"keep": 1}'
    assert views.remove_local(root) == 2
    assert views.local_views(root) == {} and not (root / "data/twitch_history").exists()


def test_mirror_mode_copies_merged_views_and_a_failure_only_warns(database, tmp_path, monkeypatch, capsys):
    root = tmp_path / "checkout"
    root.mkdir()
    payload = durable_payload()
    merge_frozen_snapshot(payload, root, source_revision="c" * 64, payload_revision="d" * 64)
    local = views.local_views(root)
    assert "data/twitch_history/2026-09-29.json" in local and "data/twitch_series/index.json" in local
    assert stored(database) == local
    assert "TWITCH_VIEWS mirror status=written" in capsys.readouterr().out
    monkeypatch.setattr(views, "sync", lambda *a, **k: (_ for _ in ()).throw(StorageError("LEASE_WAIT", "busy")))
    merge_frozen_snapshot(payload, root, source_revision="c" * 64, payload_revision="d" * 64)
    assert "Twitch views mirror not updated (LEASE_WAIT)" in capsys.readouterr().out


def test_mirror_mode_is_inert_without_the_database(tmp_path, monkeypatch):
    monkeypatch.delenv("RADAR_DATABASE_PRIMARY", raising=False)
    monkeypatch.setattr(views, "sync", lambda *a, **k: pytest.fail("no database in this mode"))
    root = tmp_path / "checkout"
    root.mkdir()
    merge_frozen_snapshot(durable_payload(), root, source_revision="c" * 64, payload_revision="d" * 64)
    with pytest.raises(ValueError):
        merge_frozen_snapshot(durable_payload(), root, source_revision="c" * 64, payload_revision="d" * 64,
                              views_mode="primary")


def test_primary_mode_reads_and_writes_the_database_and_drops_views_from_git(database, repository, monkeypatch):
    remote, _, checkout = repository
    # First publish in mirror mode puts the views into Git, as today.
    monkeypatch.setattr("radar_core.storage.primary.git_primary", lambda *a, **k: None)
    publish_snapshot(durable_payload(), SubprocessGitRepository(checkout, disposable_checkout=True), views_mode="mirror")
    tracked = git("--git-dir", remote, "ls-tree", "-r", "--name-only", "main").splitlines()
    assert "data/twitch_history/2026-09-29.json" in tracked
    # An old closing view that exists only in the database must survive the primary run.
    old_close = "data/twitch_history_close/2026-09-01.json"
    assert old_close not in stored(database)
    primary_store.publish_patch(database, VIEWS, base_generation=primary_store.read_files(database, VIEWS, paths=())[0]["generation"],
        changed={old_close: b'{"kept": true}'}, operation_id="old-close-view", projection_required=False)
    receipt = publish_snapshot(durable_payload(), SubprocessGitRepository(checkout, disposable_checkout=True),
                               views_mode="primary")
    tracked = git("--git-dir", remote, "ls-tree", "-r", "--name-only", "main").splitlines()
    assert not any(views.is_view(path) for path in tracked)
    assert "data/twitch_live.json" in tracked and "data/twitch_collection_status.json" in tracked
    assert receipt.changed is True
    after = stored(database)
    assert after[old_close] == b'{"kept": true}'
    assert json.loads(after["data/twitch_history/2026-09-29.json"])["date"] == "2026-09-29"
    assert read_remote(remote, "data/twitch_collection_status.json")["collection_complete"] is True
