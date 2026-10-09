"""Resolve the frontend Git revision and read its immutable public blobs."""

from __future__ import annotations

import json
import re
import subprocess
from urllib.request import Request, urlopen

FRONTEND = "danielet087/game-trend-radar"


def frontend_head(*, frontend=FRONTEND, run=subprocess.run, fullmatch=re.fullmatch) -> str:
    remote = (
        run(
            ["git", "ls-remote", f"https://github.com/{frontend}.git", "refs/heads/main"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        .stdout.strip()
        .split()
    )
    if (
        len(remote) != 2
        or not fullmatch("[0-9a-f]{40}", remote[0])
        or remote[1] != "refs/heads/main"
    ):
        raise ValueError("Cannot resolve current frontend HEAD for tracking state")
    return remote[0]


def read_published_json(
    commit: str,
    path: str,
    *,
    frontend=FRONTEND,
    request_type=Request,
    open_url=urlopen,
    json_module=json,
):
    url = f"https://raw.githubusercontent.com/{frontend}/{commit}/{path}"
    request = request_type(url, headers={"User-Agent": "game-trend-radar-tracking-loader"})
    with open_url(request, timeout=20) as response:
        payload = json_module.load(response)
    return payload
