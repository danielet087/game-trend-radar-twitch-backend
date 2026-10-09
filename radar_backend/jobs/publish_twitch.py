"""Publication composition: secure Git adapter and acknowledged job receipt."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Mapping

from radar_core.jobs import JobResult, JobStatus
from radar_core.publication import PublicationError, SubprocessGitRepository
from radar_backend.publication.twitch import freeze_snapshot, input_kind, publish_snapshot
from radar_backend.state.json_snapshot import write_json

FRONTEND_URL = "https://github.com/danielet087/game-trend-radar.git"
ROOT = Path(__file__).resolve().parents[2]


def run_publication_job(
    snapshot: str | Path, environment: Mapping[str, str], *, receipt_output: str | Path | None = None,
) -> dict:
    source = Path(snapshot).resolve()
    output = (Path(receipt_output) if receipt_output is not None else
              source.with_name("twitch_publication.json")).resolve()
    if output == source:
        raise ValueError("Publication receipt must not replace the collected snapshot")
    # A failed recovery attempt must not leave an older successful artifact
    # at the path the current workflow will upload.
    output.unlink(missing_ok=True)
    if not environment.get("FRONTEND_REPO_TOKEN", "").strip():
        raise SystemExit("FRONTEND_REPO_TOKEN is not configured; no data published.")
    payload = freeze_snapshot(source)
    with tempfile.TemporaryDirectory(
        prefix="radar-twitch-publish.", dir=environment.get("RUNNER_TEMP") or None,
    ) as temporary:
        frontend = Path(temporary) / "frontend"
        subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", "main", FRONTEND_URL, str(frontend)],
            check=True, env=dict(environment),
        )
        for key, value in (("user.name", "github-actions[bot]"),
                           ("user.email", "41898282+github-actions[bot]@users.noreply.github.com")):
            subprocess.run(["git", "-C", str(frontend), "config", key, value],
                           check=True, env=dict(environment))
        repository = SubprocessGitRepository(
            frontend, disposable_checkout=True,
            environment=environment,
            push_command_prefix=("bash", str(ROOT / "scripts/git_frontend_auth.sh")),
        )
        receipt = publish_snapshot(payload, repository)
    result = {
        **receipt.to_dict(),
        "input_kind": input_kind(payload),
        "job_result": JobResult(
            job="twitch_publication", status=JobStatus.COMPLETE,
            collection_complete=True, state_persisted=True, published=True,
            requires_publication=True,
            target_slot=payload.get("collection_schedule", {}).get("target_slot"),
            input_revision=receipt.input_revision,
        ).to_dict(),
    }
    # The actual accepted Git revision is available only after push. Keep it
    # outside that checkout so it cannot create a circular commit identity.
    write_json(result, output)
    if receipt.attempts > 1:
        print(f"Push rejected; retrying against latest frontend succeeded on attempt {receipt.attempts}.")
    print(f"Published Twitch snapshot at frontend {receipt.published_revision}.")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish frozen Twitch latest, history and durable inputs")
    parser.add_argument("snapshot", nargs="?", default="output/twitch_live.json")
    parser.add_argument("--receipt-output")
    args = parser.parse_args()
    try:
        run_publication_job(args.snapshot, os.environ, receipt_output=args.receipt_output)
    except (ValueError, OSError, subprocess.SubprocessError, PublicationError) as error:
        raise SystemExit(f"Twitch publication failed ({type(error).__name__}); collected JSON remains unchanged.") from None


if __name__ == "__main__":
    main()
