"""Integration checks use local Git repositories, never real platform APIs."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

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
    snapshot.write_text('{"source":"test fixture", "top_games":[]}')
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
    assert git('--git-dir',remote,'diff-tree','--no-commit-id','--name-only','-r','main') == f'data/{PLATFORM}_live.json'
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
