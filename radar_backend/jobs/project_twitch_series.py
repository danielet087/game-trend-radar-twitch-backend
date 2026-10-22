"""Rebuild history projections through the normal DB-first publication engine."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile

from radar_core.publication import SubprocessGitRepository, publish_with_retry, snapshot_revision
from radar_backend.jobs.publish_twitch import FRONTEND_URL, ROOT
from radar_backend.state.twitch_series import SERIES_PATH, project_series


def main():
    if not os.environ.get("FRONTEND_REPO_TOKEN", "").strip():
        raise SystemExit("FRONTEND_REPO_TOKEN is not configured")
    revision = snapshot_revision({"schema": 1, "code": os.environ["GITHUB_SHA"],
                                  "run": os.environ["GITHUB_RUN_ID"],
                                  "attempt": os.environ.get("GITHUB_RUN_ATTEMPT", "1")})
    with tempfile.TemporaryDirectory(prefix="radar-twitch-series.") as temporary:
        frontend = Path(temporary) / "frontend"
        subprocess.run(["git", "clone", "--depth", "1", "--branch", "main", FRONTEND_URL, str(frontend)], check=True)
        for key, value in (("user.name", "github-actions[bot]"), ("user.email", "41898282+github-actions[bot]@users.noreply.github.com")):
            subprocess.run(["git", "-C", str(frontend), "config", key, value], check=True)
        repository = SubprocessGitRepository(frontend, disposable_checkout=True, environment=os.environ,
            push_command_prefix=("bash", str(ROOT / "scripts/git_frontend_auth.sh")))
        receipt = publish_with_retry(repository, project_series, paths=(SERIES_PATH,),
            message="data: project compact Twitch game history", input_revision=revision,
            payload_revision=revision)
        print(f"Published Twitch history at frontend {receipt.published_revision}.")


if __name__ == "__main__":
    main()
