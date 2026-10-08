"""Offline publication proofs against real, disposable bare Git repositories."""
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import sys

import pytest

from radar_core.publication import SubprocessGitRepository, snapshot_revision
from radar_backend.application.collect_twitch import collect_observations
from radar_backend.domain.collection import CollectionInputs, TwitchCredentials
from radar_backend.publication.twitch import (
    freeze_snapshot, input_revision, merge_frozen_snapshot, publish_snapshot,
)
from scripts.store_twitch_snapshot import store_snapshot
from tests.test_backend_layers import MemoryInputs, request
from tests.test_standalone import git
from tests.test_steam_twitch_pipeline import mapping_state
from tests.test_twitch_history import scheduled_snapshot
from tests.test_twitch_steam_discovery_pipeline import discovery_state
from tests.test_twitch_tracking_storage import tracking_state


@pytest.fixture
def repository(tmp_path):
    remote, seed, checkout = (tmp_path / name for name in ("remote.git", "seed", "checkout"))
    git("init", "--bare", "--initial-branch=main", remote)
    git("clone", remote, seed)
    git("config", "user.name", "Test", cwd=seed)
    git("config", "user.email", "test@example.test", cwd=seed)
    (seed / "data").mkdir()
    (seed / "data/catalog.json").write_text('{"version": 1}\n')
    git("add", ".", cwd=seed)
    git("commit", "-m", "initial", cwd=seed)
    git("push", "origin", "main", cwd=seed)
    git("clone", remote, checkout)
    git("config", "user.name", "Test", cwd=checkout)
    git("config", "user.email", "test@example.test", cwd=checkout)
    return remote, seed, checkout


def durable_payload():
    payload = scheduled_snapshot()
    payload["input_revision"] = snapshot_revision({"inputs": "one frozen collection input bundle"})
    payload["tracking_state"] = tracking_state()
    payload["steam_mapping_state"] = mapping_state()
    # Production collectors already carry these independent metadata fields.
    # Legacy state may legitimately receive its existing one-time migration.
    payload["tracking_state"]["games"]["1"]["steam_matches"] = []
    payload["steam_mapping_state"]["games"]["100"]["metadata_updated_at"] = payload["steam_mapping_state"]["updated_at"]
    payload["steam_discovery_state"] = discovery_state()
    return payload


def read_remote(remote, path):
    return json.loads(git("--git-dir", remote, "show", f"main:{path}"))


def test_one_acknowledged_commit_contains_census_history_state_and_bound_receipt(repository):
    remote, _, checkout = repository
    source = durable_payload()
    before = deepcopy(source)
    receipt = publish_snapshot(source, SubprocessGitRepository(checkout, disposable_checkout=True))
    assert source == before
    assert receipt.published_revision == git("--git-dir", remote, "rev-parse", "main")
    assert receipt.input_revision == source["input_revision"]
    assert receipt.payload_revision == snapshot_revision(source)
    changed = set(git("--git-dir", remote, "diff-tree", "--no-commit-id", "--name-only", "-r", "main").splitlines())
    assert changed == {
        "data/twitch_live.json", "data/twitch_history/2026-09-29.json",
        "data/twitch_collection_status.json", "data/twitch_tracking.json",
        "data/twitch_steam_mapping.json", "data/twitch_steam_discovery.json",
    }
    status = read_remote(remote, "data/twitch_collection_status.json")
    assert status["collection_complete"] is True
    assert status["target_slot"] == source["collection_schedule"]["target_slot"]
    assert status["input_revision"] == receipt.input_revision
    assert status["input_kind"] == "collection_inputs"
    assert status["payload_revision"] == receipt.payload_revision
    assert "published_revision" not in status
    latest = read_remote(remote, "data/twitch_live.json")
    assert latest["tracking_state"] == read_remote(remote, "data/twitch_tracking.json")
    assert latest["steam_mapping_state"] == read_remote(remote, "data/twitch_steam_mapping.json")
    assert latest["steam_discovery_state"] == read_remote(remote, "data/twitch_steam_discovery.json")
    assert read_remote(remote, "data/catalog.json") == {"version": 1}


def test_noop_still_pushes_and_acknowledges_existing_remote_commit(repository, tmp_path):
    remote, _, checkout = repository
    source = durable_payload()
    first = publish_snapshot(source, SubprocessGitRepository(checkout, disposable_checkout=True))
    marker = tmp_path / "acknowledged"
    push = tmp_path / "push.py"
    push.write_text(
        "import subprocess, sys\nfrom pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('real push attempted')\n"
        "raise SystemExit(subprocess.run(['git', *sys.argv[1:]]).returncode)\n"
    )
    second = publish_snapshot(source, SubprocessGitRepository(
        checkout, disposable_checkout=True, push_command_prefix=(sys.executable, str(push)),
    ))
    assert marker.read_text() == "real push attempted"
    assert second.changed is False
    assert second.published_revision == first.published_revision == git("--git-dir", remote, "rev-parse", "main")
    assert git("--git-dir", remote, "rev-list", "--count", "main") == "2"


def test_late_publication_preserves_newest_latest_receipt_and_foreign_history(repository):
    remote, seed, checkout = repository
    newer = scheduled_snapshot("2026-09-28T18:05:00Z", "2026-09-28T18:25:00Z",
                               target="2026-09-28T18:00:00Z", run_id="102", viewers=12000)
    newer["input_revision"] = snapshot_revision({"input": "new"})
    newer["tracking_state"] = tracking_state("2", at="2026-09-28T18:25:00Z")
    store_snapshot(newer, seed)
    merge_frozen_snapshot(newer, seed, source_revision=newer["input_revision"],
                          payload_revision=snapshot_revision(newer))
    git("add", ".", cwd=seed)
    git("commit", "-m", "newer census", cwd=seed)
    git("push", "origin", "main", cwd=seed)
    latest_before = git("--git-dir", remote, "show", "main:data/twitch_live.json")
    status_before = git("--git-dir", remote, "show", "main:data/twitch_collection_status.json")
    old = durable_payload()
    publish_snapshot(old, SubprocessGitRepository(checkout, disposable_checkout=True))
    assert git("--git-dir", remote, "show", "main:data/twitch_live.json") == latest_before
    assert git("--git-dir", remote, "show", "main:data/twitch_collection_status.json") == status_before
    history = read_remote(remote, "data/twitch_history/2026-09-29.json")
    assert set(history["hours"]) == {"2026-09-28T16:00:00Z", "2026-09-28T18:00:00Z"}
    assert set(read_remote(remote, "data/twitch_tracking.json")["games"]) == {"1", "2"}
    assert read_remote(remote, "data/twitch_tracking.json")["updated_at"] == newer["generated_at"]


def test_push_rejection_retries_same_frozen_source_against_freshest_json(repository, tmp_path):
    remote, seed, checkout = repository
    source = durable_payload()
    frozen_revision = snapshot_revision(source)
    marker = tmp_path / "raced"
    newer_tracking = tracking_state("2", at="2026-09-28T18:25:00Z")
    push = tmp_path / "push.py"
    push.write_text(
        "import subprocess, sys\nfrom pathlib import Path\n"
        f"marker=Path({str(marker)!r})\nseed=Path({str(seed)!r})\n"
        "if not marker.exists():\n"
        "    marker.touch()\n"
        "    (seed/'data/catalog.json').write_text('{\"version\": 2}')\n"
        f"    (seed/'data/twitch_tracking.json').write_text({json.dumps(newer_tracking)!r})\n"
        "    for args in [('add','.'),('commit','-m','concurrent state'),('push','origin','main')]:\n"
        "        subprocess.run(['git','-C',str(seed),*args],check=True,capture_output=True)\n"
        "raise SystemExit(subprocess.run(['git', *sys.argv[1:]]).returncode)\n"
    )
    receipt = publish_snapshot(source, SubprocessGitRepository(
        checkout, disposable_checkout=True, push_command_prefix=(sys.executable, str(push)),
    ))
    assert receipt.attempts == 2
    assert receipt.payload_revision == frozen_revision == snapshot_revision(source)
    assert read_remote(remote, "data/catalog.json") == {"version": 2}
    assert set(read_remote(remote, "data/twitch_tracking.json")["games"]) == {"1", "2"}
    assert read_remote(remote, "data/twitch_live.json")["candidate_games"] == source["candidate_games"]


def test_failed_pushes_never_create_remote_success_receipt(repository, tmp_path):
    remote, _, checkout = repository
    original = git("--git-dir", remote, "rev-parse", "main")
    push = tmp_path / "reject.py"
    push.write_text("raise SystemExit(1)\n")
    with pytest.raises(Exception):
        publish_snapshot(durable_payload(), SubprocessGitRepository(
            checkout, disposable_checkout=True, push_command_prefix=(sys.executable, str(push)),
        ), max_attempts=3)
    assert git("--git-dir", remote, "rev-parse", "main") == original
    assert git("--git-dir", remote, "ls-tree", "--name-only", "main:data") == "catalog.json"


def test_legacy_revision_is_explicit_and_does_not_claim_collection_inputs(tmp_path):
    source = scheduled_snapshot()
    payload_hash = snapshot_revision(source)
    revision = input_revision(source)
    assert revision == snapshot_revision(source)
    merge_frozen_snapshot(source, tmp_path, source_revision=revision, payload_revision=payload_hash)
    receipt = json.loads((tmp_path / "data/twitch_collection_status.json").read_text())
    assert receipt["input_kind"] == "legacy_collected_snapshot"
    assert receipt["input_revision"] == revision


def test_recovery_snapshot_is_read_once_before_destination_retry(tmp_path):
    source = tmp_path / "source.json"
    source.write_text(json.dumps(durable_payload()))
    frozen = freeze_snapshot(source)
    source.write_text("{invalid replacement")
    merge_frozen_snapshot(frozen, tmp_path / "frontend", source_revision=input_revision(frozen),
                          payload_revision=snapshot_revision(frozen))
    saved = json.loads((tmp_path / "frontend/data/twitch_live.json").read_text())
    assert saved["candidate_games"] == frozen["candidate_games"]
    assert source.read_text() == "{invalid replacement"


@pytest.mark.parametrize("revision", [None, "", "G" * 64, "a" * 40, 1])
def test_malformed_explicit_input_revision_stops_before_repository_io(revision):
    payload = durable_payload()
    payload["input_revision"] = revision
    class NoGit:
        def __getattr__(self, key):
            pytest.fail("invalid source must stop before Git IO")
    with pytest.raises(ValueError):
        publish_snapshot(payload, NoGit())


def test_input_revision_records_inputs_before_collector_can_mutate_them():
    bundle = CollectionInputs({"initial": "tracking"}, {"initial": "catalog"},
                              {"initial": "mapping"}, {"initial": "discovery"})
    expected = snapshot_revision({
        "tracking_state": bundle.tracking_state, "steam_catalog": bundle.steam_catalog,
        "steam_mapping_state": bundle.steam_mapping_state,
        "steam_discovery_state": bundle.steam_discovery_state,
    })
    def collector(**kwargs):
        kwargs["tracking_state"]["initial"] = "mutated during collection"
        return {"real_observation": True}
    result = collect_observations(request(), TwitchCredentials("fixture", "fixture"),
                                  input_store=MemoryInputs(bundle), collector=collector)
    assert result["input_revision"] == expected
    assert "job_result" not in result


def test_failed_recovery_does_not_upload_a_previous_success_receipt(tmp_path, monkeypatch):
    from radar_backend.jobs import publish_twitch

    source, receipt = tmp_path / "twitch_live.json", tmp_path / "twitch_publication.json"
    source.write_text("{broken observation")
    receipt.write_text('{"published_revision": "old successful artifact"}')
    monkeypatch.setattr(publish_twitch.subprocess, "run", lambda *a, **k: pytest.fail("invalid source must stop before Git"))
    with pytest.raises(ValueError):
        publish_twitch.run_publication_job(source, {"FRONTEND_REPO_TOKEN": "fixture"})
    assert not receipt.exists()
    assert source.read_text() == "{broken observation"


def test_publication_receipt_cannot_overwrite_its_frozen_source(tmp_path):
    from radar_backend.jobs.publish_twitch import run_publication_job

    source = tmp_path / "source.json"
    raw = json.dumps(durable_payload())
    source.write_text(raw)
    with pytest.raises(ValueError, match="must not replace"):
        run_publication_job(source, {"FRONTEND_REPO_TOKEN": "fixture"}, receipt_output=source)
    assert source.read_text() == raw


@pytest.mark.parametrize("raw", [
    '{"coverage": {"collection_complete": false, "collection_complete": true}}',
    '{"unknown_metric": NaN}', '{"unknown_metric": Infinity}', 'null', '[]',
])
def test_ambiguous_or_nonfinite_source_stops_before_git(tmp_path, monkeypatch, raw):
    from radar_backend.jobs import publish_twitch

    source = tmp_path / "twitch_live.json"
    source.write_text(raw)
    monkeypatch.setattr(publish_twitch.subprocess, "run", lambda *a, **k: pytest.fail("malformed source must stop before Git"))
    with pytest.raises(ValueError):
        publish_twitch.run_publication_job(source, {"FRONTEND_REPO_TOKEN": "fixture"})
    assert not (tmp_path / "twitch_publication.json").exists()


def test_same_clock_conflicting_census_cannot_relabel_published_receipt(repository):
    remote, _, checkout = repository
    original = durable_payload()
    publish_snapshot(original, SubprocessGitRepository(checkout, disposable_checkout=True))
    commit_before = git("--git-dir", remote, "rev-parse", "main")
    status_before = git("--git-dir", remote, "show", "main:data/twitch_collection_status.json")
    conflicting = durable_payload()
    conflicting["candidate_games"][0]["viewer_count"] += 1
    conflicting["input_revision"] = snapshot_revision({"different_input": True})
    with pytest.raises(ValueError, match="Conflicting Twitch census"):
        publish_snapshot(conflicting, SubprocessGitRepository(checkout, disposable_checkout=True))
    assert git("--git-dir", remote, "rev-parse", "main") == commit_before
    assert git("--git-dir", remote, "show", "main:data/twitch_collection_status.json") == status_before
    assert read_remote(remote, "data/twitch_live.json")["candidate_games"] == original["candidate_games"]


def test_job_push_failures_leave_source_and_remote_intact_without_old_artifact(repository, tmp_path):
    from radar_core.publication import PublicationError
    from radar_backend.jobs.publish_twitch import run_publication_job

    remote, _, _ = repository
    original = git("--git-dir", remote, "rev-parse", "main")
    source = tmp_path / "twitch_live.json"
    raw = json.dumps(durable_payload())
    source.write_text(raw)
    artifact = tmp_path / "twitch_publication.json"
    artifact.write_text('{"published_revision": "old successful artifact"}')
    config = tmp_path / "gitconfig"
    config.write_text(f'[url "{remote.as_uri()}"]\n\tinsteadOf = https://github.com/danielet087/game-trend-radar.git\n')
    binary = tmp_path / "bin"
    binary.mkdir()
    attempts = tmp_path / "push-attempts"
    wrapper = binary / "git"
    real_git = shutil.which("git")
    wrapper.write_text(
        f"#!{sys.executable}\nimport os, sys\nfrom pathlib import Path\n"
        "if 'push' in sys.argv:\n"
        f"    marker=Path({str(attempts)!r})\n"
        "    marker.write_text(str(int(marker.read_text()) + 1 if marker.exists() else 1))\n"
        "    raise SystemExit(1)\n"
        f"os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])\n"
    )
    wrapper.chmod(0o755)
    environment = dict(os.environ, FRONTEND_REPO_TOKEN="fixture-only-token",
                       GIT_CONFIG_GLOBAL=str(config), GIT_CONFIG_NOSYSTEM="1",
                       RUNNER_TEMP=str(tmp_path), PATH=str(binary) + os.pathsep + os.environ["PATH"])
    with pytest.raises(PublicationError, match="not acknowledged after 5 attempts"):
        run_publication_job(source, environment)
    assert attempts.read_text() == "5"
    assert not artifact.exists()
    assert source.read_text() == raw
    assert git("--git-dir", remote, "rev-parse", "main") == original
