"""One exact metadata probe must preserve unknown values and never leak secrets."""
from copy import deepcopy
import json

import pytest
import requests

from scripts import probe_igdb_hypes as probe


def game(**changes):
    return {"id": 366896, "name": "Fire Emblem: Fortune's Weave", "hypes": 42,
            "platforms": [{"id": 508, "name": "Nintendo Switch 2"}],
            "release_dates": [{"id": 99, "platform": {"id": 508, "name": "Nintendo Switch 2"}, "human": "2026"}],
            "url": "https://www.igdb.com/games/fire-emblem-fortunes-weave", **changes}


class Response:
    status_code = 200
    def __init__(self, payload):
        self.payload = payload
    def raise_for_status(self):
        if isinstance(self.payload, Exception):
            raise self.payload
    def json(self):
        return deepcopy(self.payload)


class Client:
    def __init__(self, *args, payload=None, **kwargs):
        self.client_id, self.client_secret, self.access_token = "CLIENTSECRET", "SECRET", None
        self.timeout_seconds, self.session, self.authenticated, self.calls = 20, self, 0, []
        self.payload = [game()] if payload is None else payload
        self.monotonic = lambda: 10
    def authenticate(self):
        self.authenticated += 1
        self.access_token = "TOKEN_SECRET"
    def _wait(self):
        pass
    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return Response(self.payload)


def test_exact_single_request_and_minimal_platform_provenance():
    client = Client()
    result = probe.probe(client)
    assert client.authenticated == 1 and len(client.calls) == 1
    endpoint, call = client.calls[0]
    assert endpoint == probe.ENDPOINT and call["allow_redirects"] is False
    assert call["data"] == "fields id,name,hypes,platforms.name,release_dates.platform.name,release_dates.human,url; where id = 366896; limit 2;"
    assert result["hypes"] == 42 and result["hypes_status"] == "available"
    assert result["platforms"][0]["name"] == "Nintendo Switch 2"
    assert result["release_dates"][0]["human"] == "2026"
    assert result["source"]["endpoint"] == probe.ENDPOINT
    assert "TOKEN_SECRET" not in json.dumps(result) and "CLIENTSECRET" not in json.dumps(result)


@pytest.mark.parametrize("missing", [False, True])
def test_missing_or_null_hypes_remains_unknown(missing):
    row = game(hypes=None)
    if missing:
        row.pop("hypes")
    result = probe.validate_result([row], probe.DEFAULT_GAME_ID, "2026-10-03T15:00:00Z")
    assert result["hypes"] is None and result["hypes_status"] == "missing"
    assert ("hypes" not in result["source"]["raw_fields"]) == missing


def test_true_zero_hypes_is_available():
    result = probe.validate_result([game(hypes=0)], probe.DEFAULT_GAME_ID, "2026-10-03T15:00:00Z")
    assert result["hypes"] == 0 and result["hypes_status"] == "available"


@pytest.mark.parametrize("rows", [[], [game(), game()], [game(id=9)], [game(hypes=True)], [game(hypes=-1)], [game(hypes=1.5)]])
def test_ambiguous_wrong_identity_and_invalid_hypes_fail_closed(rows):
    with pytest.raises(ValueError):
        probe.validate_result(rows, probe.DEFAULT_GAME_ID, "2026-10-03T15:00:00Z")


def test_cli_writes_machine_readable_result_and_short_summary(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TWITCH_CLIENT_ID", "CLIENTSECRET")
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", "SECRET")
    monkeypatch.setattr(probe, "TwitchClient", Client)
    output = tmp_path / "nested/probe.json"
    assert probe.main(["--output", str(output)]) == 0
    result = json.loads(output.read_text())
    summary = json.loads(capsys.readouterr().out)
    assert result["game_id"] == probe.DEFAULT_GAME_ID and summary["hypes"] == 42
    assert "source" not in summary and "platforms" not in summary
    assert "SECRET" not in json.dumps(result) and "SECRET" not in json.dumps(summary)


def test_http429_fails_once_with_safe_classification_and_no_output_file(tmp_path, monkeypatch, capsys):
    response = requests.Response()
    response.status_code = 429
    response._content = b"SECRET authorization token body"
    failure = requests.HTTPError("SECRET Authorization Bearer TOKEN_SECRET", response=response)
    client = Client(payload=failure)
    monkeypatch.setenv("TWITCH_CLIENT_ID", "CLIENTSECRET")
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", "SECRET")
    monkeypatch.setattr(probe, "TwitchClient", lambda *args, **kwargs: client)
    output = tmp_path / "probe.json"
    assert probe.main(["--output", str(output)]) == 1
    printed = capsys.readouterr().out
    assert json.loads(printed) == {"status": "error", "stage": "lookup", "reason": "http_error", "http_status": 429}
    assert "SECRET" not in printed and not output.exists() and len(client.calls) == 1


def test_malformed_authentication_error_has_no_traceback_or_secret(tmp_path, monkeypatch, capsys):
    class BadAuth(Client):
        def authenticate(self):
            raise AttributeError("OAuth SECRET response TOKEN_SECRET")
    monkeypatch.setenv("TWITCH_CLIENT_ID", "CLIENTSECRET")
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", "SECRET")
    monkeypatch.setattr(probe, "TwitchClient", BadAuth)
    assert probe.main(["--output", str(tmp_path / "probe.json")]) == 1
    printed = capsys.readouterr()
    assert json.loads(printed.out)["reason"] == "invalid_response"
    assert "SECRET" not in printed.out and printed.err == ""


def test_missing_environment_does_not_authenticate(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("TWITCH_CLIENT_ID", raising=False)
    monkeypatch.delenv("TWITCH_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(probe, "TwitchClient", lambda *args, **kwargs: pytest.fail("credentials are required first"))
    assert probe.main(["--output", str(tmp_path / "probe.json")]) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "missing_twitch_credentials"


@pytest.mark.parametrize("value", ["0", "-1", "00366896", "３６６８９６", "366896;fields *"])
def test_cli_id_rejects_noncanonical_or_injected_values(value):
    with pytest.raises(Exception):
        probe.game_id(value)
