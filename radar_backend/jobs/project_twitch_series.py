"""Rebuild history projections through the normal DB-first publication engine.

Once the Twitch views flag moves the views to the database, the projections
are rebuilt from the database-only views source and written back there with a
compare-and-swap; nothing is published to the frontend.
"""
from __future__ import annotations

import os
import json
from pathlib import Path
import subprocess
import tempfile

from radar_core.publication import SubprocessGitRepository, publish_with_retry, snapshot_revision
from radar_backend.jobs.publish_twitch import FRONTEND_URL, ROOT
from radar_backend.adapters.twitch_http import TwitchClient
from radar_backend.state import twitch_views_store as views_store
from radar_backend.state.twitch_series import SERIES_PATH, project_series, cover_url, _write
from radar_backend.state.twitch_history_views import VIEW_PATHS, project_history_views


def restore_covers(frontend, lookup):
    """Fetch only missing category artwork; never change observation history."""
    path = frontend / SERIES_PATH / "index.json"
    index = json.loads(path.read_text())
    missing = [game_id for game_id, game in index["games"].items() if not cover_url(game.get("box_art_url"))]
    if not missing:
        return 0
    found = lookup(missing)
    restored = 0
    for game_id in missing:
        cover = cover_url(getattr(found.get(game_id), "box_art_url", None))
        if cover:
            index["games"][game_id]["box_art_url"] = cover
            restored += 1
    _write(path, index)
    return restored


def main():
    if not os.environ.get("FRONTEND_REPO_TOKEN", "").strip():
        raise SystemExit("FRONTEND_REPO_TOKEN is not configured")
    revision = snapshot_revision({"schema": 1, "code": os.environ["GITHUB_SHA"],
                                  "run": os.environ["GITHUB_RUN_ID"],
                                  "attempt": os.environ.get("GITHUB_RUN_ATTEMPT", "1")})
    client_id, client_secret = os.environ.get("TWITCH_CLIENT_ID", "").strip(), os.environ.get("TWITCH_CLIENT_SECRET", "").strip()
    client = TwitchClient(client_id, client_secret) if client_id and client_secret else None

    def project(frontend):
        project_series(frontend)
        project_history_views(frontend)
        if client:
            restored = restore_covers(frontend, client.get_games_by_ids)
            print(f"Restored {restored} missing Twitch category covers.")
    mode = views_store.mode_in_database()
    with tempfile.TemporaryDirectory(prefix="radar-twitch-series.") as temporary:
        frontend = Path(temporary) / "frontend"
        subprocess.run(["git", "clone", "--depth", "1", "--branch", "main", FRONTEND_URL, str(frontend)], check=True)
        if mode == "primary":
            # The tracking registry and other inputs still come from the frontend.
            head = views_store.materialize(frontend)
            project(frontend)
            result = views_store.sync(frontend, payload_revision=revision, base_generation=head["generation"])
            print("Twitch history projected in the database views source: "
                  + " ".join(f"{key}={value}" for key, value in result.items()))
            return
        for key, value in (("user.name", "github-actions[bot]"), ("user.email", "41898282+github-actions[bot]@users.noreply.github.com")):
            subprocess.run(["git", "-C", str(frontend), "config", key, value], check=True)
        repository = SubprocessGitRepository(frontend, disposable_checkout=True, environment=os.environ,
            push_command_prefix=("bash", str(ROOT / "scripts/git_frontend_auth.sh")))
        receipt = publish_with_retry(repository, project, paths=(SERIES_PATH, *VIEW_PATHS),
            message="data: project compact Twitch game history", input_revision=revision,
            payload_revision=revision)
        print(f"Published Twitch history at frontend {receipt.published_revision}.")


if __name__ == "__main__":
    main()
