"""The archive must hold a verified copy of every finished day before pruning."""
import gzip
import json

import pytest

from scripts.archive_twitch_history import MANIFEST, archive, finished_days


class MemoryReleases:
    def __init__(self, corrupt=False):
        self.assets, self.created, self.corrupt = {}, [], corrupt

    def ensure(self, tag, month):
        if tag not in self.assets:
            self.assets[tag] = {}
            self.created.append(tag)

    def download(self, tag, name, directory):
        raw = self.assets.get(tag, {}).get(name)
        if raw is None:
            return None
        if self.corrupt and name.endswith(".gz"):
            raw = gzip.compress(b"{}", mtime=0)
        (directory / name).write_bytes(raw)
        return raw

    def upload(self, tag, path):
        self.assets[tag][path.name] = path.read_bytes()


def write_days(root, days):
    history = root / "data/twitch_history"
    history.mkdir(parents=True, exist_ok=True)
    for day in days:
        (history / f"{day}.json").write_text(json.dumps({"schema_version": 1, "date": day, "hours": {}}), encoding="utf-8")
    return history


def test_only_days_before_the_newest_window_are_finished(tmp_path):
    history = write_days(tmp_path, ["2026-10-30", "2026-10-31", "2026-11-13", "2026-11-14", "2026-11-16"])
    assert finished_days(history, 3) == ["2026-10-30", "2026-10-31", "2026-11-13"]
    assert finished_days(history, 1) == ["2026-10-30", "2026-10-31", "2026-11-13", "2026-11-14"]
    with pytest.raises(ValueError):
        finished_days(history, 0)


def test_archive_uploads_verified_copies_per_month_and_skips_unchanged_days(tmp_path):
    history = write_days(tmp_path, ["2026-10-31", "2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"])
    releases = MemoryReleases()
    report = archive(history, releases, keep_days=3)
    assert report["archived"] == ["2026-10-31", "2026-11-01", "2026-11-02"]
    assert report["kept_on_site"] == 3
    assert releases.created == ["twitch-history-2026-10", "twitch-history-2026-11"]
    november = releases.assets["twitch-history-2026-11"]
    assert gzip.decompress(november["2026-11-01.json.gz"]) == (history / "2026-11-01.json").read_bytes()
    manifest = json.loads(november[MANIFEST])
    assert list(manifest["days"]) == ["2026-11-01", "2026-11-02"]
    assert manifest["days"]["2026-11-01"]["asset"] == "2026-11-01.json.gz"
    again = archive(history, releases, keep_days=3)
    assert again["archived"] == [] and again["unchanged"] == ["2026-10-31", "2026-11-01", "2026-11-02"]
    # A corrected day file is uploaded again and the manifest follows it.
    (history / "2026-11-01.json").write_text('{"schema_version": 1, "date": "2026-11-01", "hours": {"x": 1}}')
    assert archive(history, releases, keep_days=3)["archived"] == ["2026-11-01"]
    assert gzip.decompress(releases.assets["twitch-history-2026-11"]["2026-11-01.json.gz"]).endswith(b'{"x": 1}}')


def test_an_upload_that_does_not_verify_stops_before_the_manifest(tmp_path):
    history = write_days(tmp_path, ["2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04"])
    releases = MemoryReleases(corrupt=True)
    with pytest.raises(RuntimeError):
        archive(history, releases, keep_days=3)
    assert MANIFEST not in releases.assets["twitch-history-2026-11"]


def test_a_recreated_day_with_fewer_hours_never_replaces_its_archive(tmp_path):
    history = write_days(tmp_path, ["2026-11-02", "2026-11-03", "2026-11-04"])
    full = {"schema_version": 1, "date": "2026-11-01", "hours": {"a": 1, "b": 2}}
    (history / "2026-11-01.json").write_text(json.dumps(full))
    releases = MemoryReleases()
    archive(history, releases, keep_days=3)
    kept = releases.assets["twitch-history-2026-11"]["2026-11-01.json.gz"]
    (history / "2026-11-01.json").write_text(json.dumps({**full, "hours": {"a": 1}}))
    report = archive(history, releases, keep_days=3)
    assert report["conflicts"] == ["2026-11-01"] and report["archived"] == []
    assert releases.assets["twitch-history-2026-11"]["2026-11-01.json.gz"] == kept


def test_after_the_move_finished_days_are_read_from_the_database(tmp_path, monkeypatch):
    from radar_core.storage import documents, primary, primary_store
    from radar_core.storage.sql import SqliteClient
    from radar_backend.state import twitch_views_store as views
    from scripts.archive_twitch_history import _archive_frontend

    days = ["2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04"]
    history = write_days(tmp_path / "views", days)
    client = SqliteClient(tmp_path / "radar.sqlite")
    documents.initialize(client)
    primary_store.bootstrap_primary(client, views.REPOSITORY + "@" + views.BRANCH,
                                    {f"data/twitch_history/{p.name}": p.read_bytes() for p in history.glob("*.json")},
                                    "seed")
    monkeypatch.setattr(primary, "client", lambda: client)
    frontend = tmp_path / "frontend"
    write_days(frontend, ["2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04"])
    releases = MemoryReleases()
    mirrored = _archive_frontend(frontend, releases, 3)
    assert mirrored["source"] == "frontend" and mirrored["archived"] == ["2026-10-01"]
    (frontend / views.FLAG).parent.mkdir(parents=True)
    (frontend / views.FLAG).write_bytes(views.flag_document(generation=1, files=4, raw_bytes=1, moved_at="x"))
    moved = _archive_frontend(frontend, releases, 3)
    assert moved["source"] == "database" and moved["generation"] == 1 and moved["archived"] == ["2026-11-01"]
    assert gzip.decompress(releases.assets["twitch-history-2026-11"]["2026-11-01.json.gz"]) == \
        (history / "2026-11-01.json").read_bytes()
    client.close()
