#!/usr/bin/env bash
# Preserve the historical CLI while Python owns the acknowledged publication.
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source_file="$(realpath "${1:-output/twitch_live.json}")"
cd "$script_dir/.."
exec python -m radar_backend.jobs.publish_twitch "$source_file"
