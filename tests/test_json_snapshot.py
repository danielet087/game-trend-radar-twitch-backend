import json

import pytest

from radar_backend.state.json_snapshot import COMPACT_FILES, render_json, write_json

PAYLOAD = {"games": {"1": {"name": "遊戲", "viewers": [1, 2]}}, "updated_at": "2026-11-17T08:00:00Z"}


@pytest.mark.parametrize("name", sorted(COMPACT_FILES))
def test_hourly_program_files_are_written_without_indentation(tmp_path, name):
    path = write_json(PAYLOAD, tmp_path / "data" / name)
    text = path.read_text(encoding="utf-8")
    assert text == '{"games":{"1":{"name":"遊戲","viewers":[1,2]}},"updated_at":"2026-11-17T08:00:00Z"}\n'
    assert json.loads(text) == PAYLOAD


@pytest.mark.parametrize("name", ["twitch_steam_discovery.json", "twitch_collection_status.json", "report.json"])
def test_other_files_keep_readable_indentation(tmp_path, name):
    text = write_json(PAYLOAD, tmp_path / name).read_text(encoding="utf-8")
    assert text == json.dumps(PAYLOAD, ensure_ascii=False, indent=2) + "\n"


def test_offline_rendering_can_reject_non_finite_numbers():
    with pytest.raises(ValueError):
        render_json({"bad": float("nan")}, "data/twitch_live.json", allow_nan=False)
    with pytest.raises(ValueError):
        render_json({"bad": float("inf")}, "data/twitch_steam_discovery.json", allow_nan=False)
