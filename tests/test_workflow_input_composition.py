"""Exercise the production input command with legacy collectors unavailable."""

import json
from pathlib import Path
import shlex
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]

CASES = [
    ("complete", [], None, None),
    ("missing-mapping", ["mapping"], None, None),
    ("missing-discovery", ["discovery"], None, None),
    ("missing-both-optional", ["mapping", "discovery"], None, None),
    ("missing-tracking", [], "tracking", "missing"),
    ("missing-catalog", [], "catalog", "missing"),
    ("invalid-tracking", [], "tracking", "invalid"),
    ("invalid-catalog", [], "catalog", "invalid"),
    ("invalid-mapping", [], "mapping", "invalid"),
    ("invalid-discovery", [], "discovery", "invalid"),
    ("mapping-rate-limit", [], "mapping", 429),
    ("discovery-unavailable", [], "discovery", 500),
    ("corrupt-tracking", [], "tracking", "json"),
    ("corrupt-catalog", [], "catalog", "json"),
    ("corrupt-mapping", [], "mapping", "json"),
    ("corrupt-discovery", [], "discovery", "json"),
]

RUN_INPUTS = r"""
from copy import deepcopy
from datetime import datetime, timezone
import importlib.abc
import io
import json
from pathlib import Path
import socket
import sys
from types import SimpleNamespace
from urllib.error import HTTPError

class BlockCollectors(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "collectors" or fullname.startswith("collectors."):
            raise AssertionError("legacy dependency: " + fullname)

sys.meta_path.insert(0, BlockCollectors())

def no_network(*args, **kwargs):
    raise AssertionError("the workflow input test attempted live network access")

socket.create_connection = no_network
from scripts import load_twitch_tracking as loader
from radar_backend.adapters import steam_twitch_mapping as mapping
import requests
requests.sessions.Session.request = no_network

case = json.loads(sys.argv[1])
directory = Path(sys.argv[2])
commit = "b" * 40
stamp = "2026-10-01T12:00:00Z"
now = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
events = []
paths = {
    "tracking": loader.TRACKING_PATH,
    "catalog": loader.STEAM_PATH,
    "mapping": loader.MAPPING_PATH,
    "discovery": loader.DISCOVERY_PATH,
}
documents = {
    "tracking": {"schema_version": 1, "updated_at": stamp, "games": {}},
    "catalog": {
        "version": 2,
        "generated_at": stamp,
        "count": 1,
        "games": [{
            "appid": 100,
            "name": "Steam game",
            "followers": 5000,
            "release_precision": "day",
            "release_start": "2026-10-01",
            "release_end": "2026-10-01",
            "tags": ["Action"],
        }],
    },
    "mapping": {"schema_version": 1, "updated_at": stamp, "games": {}},
    "discovery": {"schema_version": 1, "updated_at": stamp, "games": {}},
}
if case.get("fault") == "invalid":
    invalid = {
        "tracking": None,
        "catalog": {**documents["catalog"], "count": 2},
        "mapping": {**documents["mapping"], "games": []},
        "discovery": None,
    }
    documents[case["path"]] = invalid[case["path"]]
for name, document in documents.items():
    content = b"{" if case.get("fault") == "json" and case["path"] == name else json.dumps(document).encode()
    (directory / (name + ".json")).write_bytes(content)
before = {path: path.read_bytes() for path in directory.glob("*.json")}

def resolve_head(command, **options):
    events.append(("head",))
    assert command == ["git", "ls-remote", "https://github.com/" + loader.FRONTEND + ".git", "refs/heads/main"]
    assert options == {"check": True, "capture_output": True, "text": True, "timeout": 30}
    return SimpleNamespace(stdout=commit + "\trefs/heads/main\n")

def read_local(request, **options):
    prefix = "https://raw.githubusercontent.com/" + loader.FRONTEND + "/" + commit + "/"
    assert request.full_url.startswith(prefix)
    path = request.full_url[len(prefix):]
    name = next(name for name, relative in paths.items() if relative == path)
    events.append(("read", name, commit))
    assert options == {"timeout": 20}
    assert request.headers == {"User-agent": "game-trend-radar-tracking-loader"}
    if name in case.get("missing", []) or name == case.get("path") and case.get("fault") == "missing":
        raise HTTPError(request.full_url, 404, "not found", {}, None)
    if name == case.get("path") and type(case.get("fault")) is int:
        raise HTTPError(request.full_url, case["fault"], "failure", {}, None)
    return io.BytesIO((directory / (name + ".json")).read_bytes())

class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        events.append(("clock",))
        assert tz is timezone.utc
        return now

def observe_validation(name, validator):
    def validate(payload):
        events.append(("validate", name))
        return validator(payload)
    return validate

normalize = mapping.normalize_steam_catalog

def observe_catalog(payload, clock):
    events.append(("normalize", clock.isoformat()))
    rows = normalize(payload, clock)
    assert rows[0]["steam_appid"] == "100"
    assert rows[0]["is_recent"] is True
    assert rows[0]["release_at"] == "2026-09-30T16:00:00Z"
    return rows

loader.subprocess.run = resolve_head
loader.urlopen = read_local
loader.datetime = FrozenDateTime
mapping.normalize_steam_catalog = observe_catalog
for name in ("tracking", "mapping", "discovery"):
    attribute = "validate_persisted_" + name
    setattr(loader, attribute, observe_validation(name, getattr(loader, attribute)))

full_order = [
    ("head",),
    ("read", "tracking", commit),
    ("validate", "tracking"),
    ("read", "catalog", commit),
    ("clock",),
    ("normalize", now.isoformat()),
    ("read", "mapping", commit),
    ("validate", "mapping"),
    ("read", "discovery", commit),
    ("validate", "discovery"),
]
if case.get("path"):
    try:
        loader.load_published_inputs()
    except Exception as error:
        fault = case["fault"]
        if fault == "invalid":
            assert type(error) is ValueError, (type(error), str(error))
            last = ("normalize", now.isoformat()) if case["path"] == "catalog" else ("validate", case["path"])
        else:
            last = ("read", case["path"], commit)
            if fault == "json":
                assert type(error) is json.JSONDecodeError, (type(error), str(error))
            else:
                assert type(error) is HTTPError, (type(error), str(error))
                assert error.code == (404 if fault == "missing" else fault)
        assert events == full_order[:full_order.index(last) + 1], events
    else:
        raise AssertionError("invalid or missing input was accepted")
elif case.get("cli"):
    sys.argv = ["loader", *case["cli"]]
    loader.main()
    output = directory / "output"
    assert json.loads((output / "twitch_tracking_input.json").read_text()) == documents["tracking"]
    assert json.loads((output / "steam_catalog_input.json").read_text()) == {**documents["catalog"], "_source_commit": commit}
    assert json.loads((output / "twitch_steam_mapping_input.json").read_text()) == documents["mapping"]
    assert json.loads((output / "twitch_steam_discovery_input.json").read_text()) == documents["discovery"]
    assert events == full_order, events
else:
    result = loader.load_published_inputs()
    assert result["source_commit"] == commit
    assert result["tracking_state"] == documents["tracking"]
    assert result["steam_catalog"] == {**documents["catalog"], "_source_commit": commit}
    for name in ("mapping", "discovery"):
        expected = {"schema_version": 1, "updated_at": None, "games": {}} if name in case["missing"] else documents[name]
        assert result["steam_" + name + "_state"] == expected
    assert events == full_order, events
assert {path: path.read_bytes() for path in directory.glob("*.json")} == before
assert not any(name == "collectors" or name.startswith("collectors.") for name in sys.modules)
"""


def run_inputs(case, tmp_path):
    result = subprocess.run(
        [sys.executable, "-c", RUN_INPUTS, json.dumps(case), str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.parametrize(
    "name,missing,path,fault", CASES, ids=[case[0] for case in CASES]
)
def test_workflow_bundle_reads_one_revision_without_legacy_collectors(
    tmp_path, name, missing, path, fault
):
    run_inputs({"missing": missing, "path": path, "fault": fault}, tmp_path)


def test_production_workflow_input_arguments_write_the_validated_bundle(tmp_path):
    workflow = (ROOT / ".github/workflows/collect.yml").read_text()
    command = next(
        line.strip().removeprefix("run: ")
        for line in workflow.splitlines()
        if "run: python -m scripts.load_twitch_tracking " in line
    )
    arguments = shlex.split(command)[3:]
    arguments = [
        str(tmp_path / argument) if argument.startswith("output/") else argument
        for argument in arguments
    ]
    stdout = run_inputs({"missing": [], "cli": arguments}, tmp_path)
    assert stdout == (
        "Loaded frontend "
        + "b" * 40
        + ": 0 tracking entries, 1 Steam games and 0 mappings and 0 Steam intake discoveries\n"
    )
