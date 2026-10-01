"""Integration checks use local Git repositories, never real platform APIs."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from tests.test_twitch_tracking_storage import tracking_state
from tests.test_steam_twitch_pipeline import mapping_state

PLATFORM = 'twitch'
ROOT = Path(__file__).resolve().parents[1]


def git(*args, cwd=None):
    return subprocess.run(['git', *map(str, args)], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def test_publish_retry_preserves_concurrent_frontend_changes(tmp_path):
    remote = tmp_path/'remote.git'
    seed = tmp_path/'seed'
    git('init', '--bare', '--initial-branch=main', remote)
    git('clone', remote, seed)
    git('config', 'user.name', 'Test', cwd=seed)
    git('config', 'user.email', 'test@example.test', cwd=seed)
    (seed/'data').mkdir()
    (seed/'data/catalog.json').write_text('{"version": 1}')
    git('add', '.', cwd=seed)
    git('commit', '-m', 'initial', cwd=seed)
    git('push', 'origin', 'main', cwd=seed)
    config = tmp_path/'gitconfig'
    config.write_text(f'[url "{remote.as_uri()}"]\n\tinsteadOf = https://github.com/danielet087/game-trend-radar.git\n')
    snapshot = tmp_path/f'{PLATFORM}_live.json'
    registry = tracking_state(at="2026-09-28T16:30:00Z")
    concurrent_registry = tracking_state("2", at="2026-09-28T16:31:00Z")
    mapping = mapping_state(at="2026-09-28T16:30:00Z")
    concurrent_mapping = mapping_state("101", at="2026-09-28T16:31:00Z", game_id="2")
    snapshot.write_text(json.dumps({
        "schema_version": 2, "generated_at": "2026-09-28T16:30:00Z", "collection_started_at": "2026-09-28T16:29:00Z",
        "min_viewers": 7000, "coverage": {"collection_complete": True, "stop_reason": "category_directory_exhausted"},
        "candidate_games": [], "top_games": [], "tracking_state": registry, "steam_mapping_state": mapping,
    }))
    real_git = shutil.which('git')
    wrapper_dir = tmp_path/'bin'
    wrapper_dir.mkdir()
    wrapper = wrapper_dir/'git'
    wrapper.write_text(f'''#!{sys.executable}
import os, subprocess, sys
from pathlib import Path
real={real_git!r}
marker=Path({str(tmp_path/'raced')!r})
seed=Path({str(seed)!r})
if 'push' in sys.argv and not marker.exists():
    marker.touch()
    (seed/'data/catalog.json').write_text('{{"version": 2}}')
    (seed/'data/other-live.json').write_text('{{"keep": true}}')
    (seed/'data/twitch_tracking.json').write_text({json.dumps(concurrent_registry)!r})
    (seed/'data/twitch_steam_mapping.json').write_text({json.dumps(concurrent_mapping)!r})
    for args in [('add','.'),('commit','-m','concurrent frontend update'),('push','origin','main')]:
        subprocess.run([real,'-C',str(seed),*args],check=True,capture_output=True)
os.execv(real,[real,*sys.argv[1:]])
''')
    wrapper.chmod(0o755)
    env = dict(os.environ, FRONTEND_REPO_TOKEN='fixture-only-token',
               GIT_CONFIG_GLOBAL=str(config), GIT_CONFIG_NOSYSTEM='1',
               RUNNER_TEMP=str(tmp_path), PATH=str(wrapper_dir)+os.pathsep+os.environ['PATH'])
    run = subprocess.run(['bash', str(ROOT/'scripts/publish_frontend.sh'), str(snapshot)],
                         env=env, capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stdout+run.stderr
    assert 'retrying against latest frontend' in run.stdout
    assert git('--git-dir',remote,'show','main:data/catalog.json') == '{"version": 2}'
    assert git('--git-dir',remote,'show','main:data/other-live.json') == '{"keep": true}'
    assert json.loads(git('--git-dir',remote,'show',f'main:data/{PLATFORM}_live.json'))['top_games'] == []
    changed = set(git('--git-dir',remote,'diff-tree','--no-commit-id','--name-only','-r','main').splitlines())
    assert changed == {'data/twitch_live.json', 'data/twitch_history/2026-09-29.json', 'data/twitch_tracking.json', 'data/twitch_steam_mapping.json'}
    history = json.loads(git('--git-dir',remote,'show','main:data/twitch_history/2026-09-29.json'))
    assert len(history['hours']) == 1
    saved_registry = json.loads(git('--git-dir',remote,'show','main:data/twitch_tracking.json'))
    assert set(saved_registry['games']) == {'1', '2'}
    assert saved_registry['updated_at'] == concurrent_registry['updated_at']
    saved_mapping = json.loads(git('--git-dir',remote,'show','main:data/twitch_steam_mapping.json'))
    assert set(saved_mapping['games']) == {'100', '101'}
    assert saved_mapping['updated_at'] == concurrent_mapping['updated_at']
    saved_latest = json.loads(git('--git-dir',remote,'show','main:data/twitch_live.json'))
    assert saved_latest['steam_mapping_state'] == saved_mapping
    assert 'fixture-only-token' not in run.stdout+run.stderr


def test_missing_publish_token_does_not_start_git(tmp_path):
    snapshot = tmp_path/'snapshot.json'
    snapshot.write_text('{}')
    env = {k:v for k,v in os.environ.items() if k!='FRONTEND_REPO_TOKEN'}
    result = subprocess.run(['bash',str(ROOT/'scripts/publish_frontend.sh'),str(snapshot)],
                            env=env,capture_output=True,text=True,timeout=5)
    assert result.returncode != 0
    assert 'FRONTEND_REPO_TOKEN is not configured' in result.stderr


def test_cli_rejects_missing_platform_credentials_before_collection(monkeypatch):
    import importlib
    module = importlib.import_module(f'scripts.update_{PLATFORM}')
    for key in ('YOUTUBE_API_KEY','TWITCH_CLIENT_ID','TWITCH_CLIENT_SECRET','STEAM_API_KEY','FRONTEND_REPO_TOKEN'):
        monkeypatch.delenv(key,raising=False)
    monkeypatch.setattr(sys,'argv',['collector'])
    monkeypatch.setattr(module,f'collect_{PLATFORM}',lambda **kwargs: pytest.fail('Must not call API'))
    with pytest.raises(SystemExit,match='required'):
        module.main()


def test_production_cli_requires_registry_before_api_calls(monkeypatch):
    from scripts import update_twitch

    monkeypatch.setenv('TWITCH_CLIENT_ID', 'fixture-client')
    monkeypatch.setenv('TWITCH_CLIENT_SECRET', 'fixture-secret')
    monkeypatch.setenv('GITHUB_RUN_ID', '123')
    monkeypatch.setattr(sys, 'argv', ['collector'])
    monkeypatch.setattr(update_twitch, 'collect_twitch', lambda **kwargs: pytest.fail('Must not call API'))
    with pytest.raises(SystemExit, match='tracking-state'):
        update_twitch.main()


def test_cli_passes_persisted_registry_to_collector(tmp_path, monkeypatch):
    from scripts import update_twitch

    state = tracking_state()
    path = tmp_path / 'state.json'
    path.write_text(json.dumps(state))
    monkeypatch.setenv('TWITCH_CLIENT_ID', 'fixture-client')
    monkeypatch.setenv('TWITCH_CLIENT_SECRET', 'fixture-secret')
    # This fixture exercises the local diagnostic CLI, even when pytest itself
    # runs inside GitHub Actions. Production requirements are tested below.
    monkeypatch.delenv('GITHUB_RUN_ID', raising=False)
    monkeypatch.setattr(sys, 'argv', ['collector', '--tracking-state', str(path)])

    def stop_after_validation(**kwargs):
        from scripts.load_twitch_tracking import validate_persisted_tracking
        assert kwargs['tracking_state'] == validate_persisted_tracking(state)
        raise RuntimeError('collector received persisted enrollments')

    monkeypatch.setattr(update_twitch, 'collect_twitch', stop_after_validation)
    with pytest.raises(RuntimeError, match='persisted enrollments'):
        update_twitch.main()


def test_production_cli_requires_steam_bundle_before_api_calls(tmp_path, monkeypatch):
    from scripts import update_twitch

    path = tmp_path / 'tracking.json'
    path.write_text(json.dumps(tracking_state()))
    monkeypatch.setenv('TWITCH_CLIENT_ID', 'fixture-client')
    monkeypatch.setenv('TWITCH_CLIENT_SECRET', 'fixture-secret')
    monkeypatch.setenv('GITHUB_RUN_ID', '123')
    monkeypatch.setattr(sys, 'argv', ['collector', '--tracking-state', str(path)])
    monkeypatch.setattr(update_twitch, 'collect_twitch', lambda **kwargs: pytest.fail('Must not call API'))
    with pytest.raises(SystemExit, match='steam-catalog and --steam-mapping'):
        update_twitch.main()


def test_cli_passes_validated_steam_catalog_and_id_mapping(tmp_path, monkeypatch):
    from scripts import update_twitch
    from tests.test_steam_twitch_pipeline import mapping_state, steam_catalog

    state, catalog, mapping = tracking_state(), steam_catalog(), mapping_state()
    paths = {}
    for name, data in [('tracking', state), ('catalog', catalog), ('mapping', mapping)]:
        paths[name] = tmp_path / f'{name}.json'
        paths[name].write_text(json.dumps(data))
    monkeypatch.setenv('TWITCH_CLIENT_ID', 'fixture-client')
    monkeypatch.setenv('TWITCH_CLIENT_SECRET', 'fixture-secret')
    monkeypatch.setenv('GITHUB_RUN_ID', '123')
    monkeypatch.setattr(sys, 'argv', ['collector', '--tracking-state', str(paths['tracking']),
                                    '--steam-catalog', str(paths['catalog']), '--steam-mapping', str(paths['mapping'])])
    def collect(**kwargs):
        assert kwargs['steam_catalog'] == catalog
        assert kwargs['steam_mapping_state'] == mapping
        raise RuntimeError('validated dual inputs delivered')
    monkeypatch.setattr(update_twitch, 'collect_twitch', collect)
    with pytest.raises(RuntimeError, match='validated dual inputs delivered'):
        update_twitch.main()
