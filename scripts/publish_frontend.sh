#!/usr/bin/env bash
# Merge only Twitch latest/history against the latest frontend branch.
set -euo pipefail
source_file="$(realpath "${1:-output/twitch_live.json}")"
test -s "$source_file"
python -m json.tool "$source_file" > /dev/null
if [[ -z "${FRONTEND_REPO_TOKEN:-}" ]]; then
  echo 'FRONTEND_REPO_TOKEN is not configured; no data published.' >&2
  exit 1
fi
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# Only the fresh temporary clone below is reset during conflict recovery.
work_dir="$(mktemp -d "${RUNNER_TEMP:-/tmp}/radar-twitch-publish.XXXXXXXX")"
trap 'rm -rf "$work_dir"' EXIT
git clone --depth 1 --branch main https://github.com/danielet087/game-trend-radar.git "$work_dir/frontend"
git -C "$work_dir/frontend" config user.name 'github-actions[bot]'
git -C "$work_dir/frontend" config user.email '41898282+github-actions[bot]@users.noreply.github.com'
for attempt in 1 2 3 4 5; do
  git -C "$work_dir/frontend" fetch origin main
  git -C "$work_dir/frontend" reset --hard origin/main
  mkdir -p "$work_dir/frontend/data"
  history_path="$(cd "$script_dir/.." && python -m scripts.store_twitch_snapshot "$source_file" "$work_dir/frontend")"
  git -C "$work_dir/frontend" add -- data/twitch_live.json "$history_path"
  if git -C "$work_dir/frontend" diff --cached --quiet; then
    echo 'twitch data is already up to date.'
    exit 0
  fi
  git -C "$work_dir/frontend" commit -m 'data: update twitch live metrics'
  if bash "$script_dir/git_frontend_auth.sh" -C "$work_dir/frontend" push origin HEAD:main; then
    echo "Published data/twitch_live.json and $history_path."
    exit 0
  fi
  echo "Push failed on attempt $attempt; retrying against latest frontend."
done
echo 'Publish failed after 5 attempts. The collected JSON remains in the workflow artifact.' >&2
exit 1
