"""The one-time move: both copies must match before the views leave Git."""
import json
import subprocess

import pytest

from radar_core.publication import SubprocessGitRepository, publish_with_retry
from radar_core.storage import primary_store
from radar_backend.publication.twitch import publish_snapshot
from radar_backend.state import twitch_views_store as views
from scripts import move_twitch_views as move
from tests.test_standalone import git
from tests.test_twitch_publication import durable_payload, repository  # noqa: F401
from tests.test_twitch_views_store import SEED, VIEWS, database, stored  # noqa: F401

FRONTEND = views.REPOSITORY + "@" + views.FRONTEND_BRANCH


def tracked(remote):
    return git("--git-dir", remote, "ls-tree", "-r", "--name-only", "main").splitlines()


def blob(remote, path):
    return subprocess.run(["git", "--git-dir", str(remote), "show", f"main:{path}"],
                          check=True, capture_output=True).stdout


def run_move(checkout, reports):
    return publish_with_retry(
        SubprocessGitRepository(checkout, disposable_checkout=True),
        lambda root: move.cutover(root, payload_revision="1" * 64, reports=reports),
        paths=(*views.PREFIXES, views.FLAG), message="data: move the Twitch views",
        input_revision="move", payload_revision="1" * 64)


def test_the_move_copies_missed_changes_then_takes_the_views_out_of_git(database, repository, monkeypatch, capsys):
    remote, _, checkout = repository
    monkeypatch.setattr("radar_core.storage.primary.git_primary", lambda *a, **k: None)
    publish_snapshot(durable_payload(), SubprocessGitRepository(checkout, disposable_checkout=True))
    in_git = {path: blob(remote, path) for path in tracked(remote) if views.is_view(path)}
    assert in_git and "TWITCH_VIEWS mirror status=written" in capsys.readouterr().out
    # A mirror write that was missed: the frontend copy is still authoritative.
    index = "data/twitch_series/index.json"
    head = primary_store.read_files(database, VIEWS, paths=())[0]
    primary_store.publish_patch(database, VIEWS, base_generation=head["generation"],
                                changed={index: b'{"schema_version": 1, "stale": true}'},
                                operation_id="missed", projection_required=False)

    reports = []
    assert run_move(checkout, reports).changed is True
    report = reports[-1]
    assert report["status"] == "moved" and report["differences_before"]["different"] == 1
    assert report["copied"]["status"] == "written" and report["removed_from_frontend"] == len(in_git)
    after = tracked(remote)
    assert views.FLAG in after and not any(views.is_view(path) for path in after)
    flag = json.loads(git("--git-dir", remote, "show", f"main:{views.FLAG}"))
    assert flag["home"] == "database" and flag["source"] == views.SOURCE and flag["files"] == len(in_git)
    assert {path: stored(database)[path] for path in in_git} == in_git

    # The next collection follows the flag: the views stay in the database only.
    publish_snapshot(durable_payload(), SubprocessGitRepository(checkout, disposable_checkout=True))
    assert "TWITCH_VIEWS primary status=unchanged" in capsys.readouterr().out
    assert not any(views.is_view(path) for path in tracked(remote))

    # A stale writer that puts views back cannot remove the flag; moving again drops them.
    seed = checkout.parent / "stale"
    git("clone", remote, seed)
    (seed / "data/twitch_series").mkdir(parents=True)
    (seed / index).write_text('{"schema_version": 1, "tiny": true}\n')
    git("-c", "user.name=Stale", "-c", "user.email=stale@example.test", "-C", seed, "add", ".")
    git("-c", "user.name=Stale", "-c", "user.email=stale@example.test", "-C", seed, "commit", "-m", "stale")
    git("-C", seed, "push", "origin", "main")
    again = []
    run_move(checkout, again)
    assert again[-1] == {"status": "already_moved", "removed_stale_views": 1}
    assert not any(views.is_view(path) for path in tracked(remote))
    assert stored(database)[index] == in_git[index]

    # Restore undoes the move: the database copy returns to Git and the flag goes.
    back = []
    publish_with_retry(SubprocessGitRepository(checkout, disposable_checkout=True),
                       lambda root: move.restore(root, reports=back),
                       paths=(*views.PREFIXES, views.FLAG), message="data: return the Twitch views",
                       input_revision="restore", payload_revision="2" * 64)
    assert back[-1]["status"] == "restored" and back[-1]["files"] == len(stored(database))
    restored = tracked(remote)
    assert views.FLAG not in restored
    assert {path: blob(remote, path) for path in restored if views.is_view(path)} == stored(database)
    publish_snapshot(durable_payload(), SubprocessGitRepository(checkout, disposable_checkout=True))
    assert "TWITCH_VIEWS mirror status=unchanged" in capsys.readouterr().out
    twice = []
    move.restore(checkout, reports=twice)
    assert twice[-1]["status"] == "not_moved"


def test_the_move_refuses_a_frontend_without_views(database, tmp_path):
    with pytest.raises(ValueError):
        move.cutover(tmp_path, payload_revision="1" * 64, reports=[])


def test_the_dry_run_compares_both_database_copies(database):
    frontend_files = {**SEED, "data/twitch_history_close/2026-09-27.json": b'{"only": "frontend"}',
                      "data/catalog.json": b"{}"}
    frontend_files["data/twitch_series/index.json"] = b'{"schema_version": 1, "newer": true}'
    primary_store.bootstrap_primary(database, FRONTEND, frontend_files, "frontend")
    report = move.compare_in_database()
    assert report["mode"] == "mirror" and report["frontend_files"] == 3 and report["views_files"] == 2
    assert (report["only_frontend"], report["only_database"], report["different"]) == (1, 0, 1)
    assert report["examples"]["different"] == ["data/twitch_series/index.json"]


def test_the_move_through_the_real_database_bridge(database, repository, monkeypatch, capsys):
    """Production path: the frontend source itself is database-primary."""
    from radar_core.storage.primary import GitPrimary

    remote, seed, checkout = repository
    primary_store.bootstrap_primary(database, FRONTEND, {"data/catalog.json": b'{"version": 1}\n'}, "frontend")

    def bridged():
        return SubprocessGitRepository(checkout, disposable_checkout=True,
                                       primary=GitPrimary(FRONTEND, db=database))

    def frontend_views():
        return {path for path in primary_store.read_files(database, FRONTEND)[1] if views.is_view(path)}

    publish_snapshot(durable_payload(), bridged())
    assert frontend_views() and "TWITCH_VIEWS mirror status=written" in capsys.readouterr().out
    held = set(stored(database))
    assert frontend_views() == held

    reports = []
    publish_with_retry(bridged(), lambda root: move.cutover(root, payload_revision="3" * 64, reports=reports),
                       paths=(*views.PREFIXES, views.FLAG), message="data: move the Twitch views",
                       input_revision="move", payload_revision="3" * 64)
    assert reports[-1]["status"] == "moved" and reports[-1]["copied"] == {"status": "identical"}
    assert frontend_views() == set() and views.mode_in_database() == "primary"
    assert not any(views.is_view(path) for path in tracked(remote)) and views.FLAG in tracked(remote)

    # A primary run rebuilds a view the database lost and writes only there.
    compact = "data/twitch_history_compact/2026-09-29.json"
    lost = stored(database)[compact]
    views.delete([compact], base_generation=views.read(())[0]["generation"], payload_revision="5" * 64)
    publish_snapshot(durable_payload(), bridged())
    assert "TWITCH_VIEWS primary status=written" in capsys.readouterr().out
    assert stored(database)[compact] == lost and set(stored(database)) == held
    assert frontend_views() == set() and not any(views.is_view(path) for path in tracked(remote))

    back = []
    publish_with_retry(bridged(), lambda root: move.restore(root, reports=back),
                       paths=(*views.PREFIXES, views.FLAG), message="data: return the Twitch views",
                       input_revision="restore", payload_revision="4" * 64)
    assert back[-1]["status"] == "restored" and frontend_views() == held
    assert views.mode_in_database() == "mirror" and views.FLAG not in tracked(remote)
