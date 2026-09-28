from datetime import datetime, timezone
import json

import pytest
import requests

from collectors.twitch_candidates import (
    IncompleteCollection, PageReader, REGISTRY_PATH, category_metrics, collect_candidates,
    load_verifications, release_hints, verification_for,
)

NOW = datetime(2026, 9, 28, 17, tzinfo=timezone.utc)


def stream(game, user, viewers):
    return {"game_id": str(game), "user_id": str(user), "viewer_count": viewers, "type": "live", "language": "zh"}


def page(rows, cursor=None):
    return {"data": rows, "pagination": {"cursor": cursor} if cursor else {}}


def game(game_id, igdb_id=""):
    return {"id": str(game_id), "name": f"Game {game_id}", "igdb_id": igdb_id}


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, endpoint, *, params):
        self.calls.append((endpoint, params))
        expected, response = self.responses.pop(0)
        assert endpoint == expected
        return response


def registry(tmp_path, observations=None):
    path = tmp_path / "verification.json"
    path.write_text(json.dumps({"schema_version": 1, "observations": observations or {}}))
    return path


def observed(status, **changes):
    return dict({"status": status, "source": "twitch_directory_dom", "source_url": "https://www.twitch.tv/directory?sort=VIEWER_COUNT",
                 "observed_at": "2026-09-28T16:00:00Z", "expires_at": "2026-09-29T16:00:00Z"}, **changes)


@pytest.mark.parametrize("counts,expected", [([100, 200, 300, 400, 9000], 300), ([0, 1, 2000, 12000], 1000.5), ([], None)])
def test_median_uses_every_channel_including_zero_viewers(counts, expected):
    client = FakeClient([("streams", page([stream(1, i + 1, count) for i, count in enumerate(counts)]))])
    metrics = category_metrics(PageReader(client, 10), "1")
    assert metrics["viewer_count"] == sum(counts)
    assert metrics["streamer_count"] == len(counts)
    assert metrics["median_viewer_count"] == expected


def test_short_page_with_cursor_continues_and_deduplicates_broadcasters():
    client = FakeClient([
        ("streams", page([stream(1, 10, 9000), stream(1, 20, 100)], "next")),
        ("streams", page([stream(1, 10, 8000), stream(1, 30, 0)])),
    ])
    result = category_metrics(PageReader(client, 10), "1")
    assert client.calls[1][1]["after"] == "next"
    assert result["viewer_count"] == 8100
    assert result["streamer_count"] == 3
    assert result["median_viewer_count"] == 100
    assert result["duplicate_broadcasters_removed"] == 1


@pytest.mark.parametrize("case", ["page_limit", "repeated_cursor", "bad_count", "wrong_game", "missing_broadcaster", "not_live", "bad_response", "budget"])
def test_incomplete_data_never_looks_like_a_complete_measurement(case):
    row = stream(1, 1, 7000)
    rows = [("streams", page([row], "next"))] * 2
    max_pages, budget = 2, 10
    if case == "page_limit":
        max_pages = 1
    elif case == "bad_count":
        row["viewer_count"] = None
    elif case == "wrong_game":
        row["game_id"] = "2"
    elif case == "missing_broadcaster":
        row.pop("user_id")
    elif case == "not_live":
        row["type"] = ""
    elif case == "bad_response":
        rows = [("streams", {"data": {}})]
    elif case == "budget":
        budget = 1
    with pytest.raises(IncompleteCollection):
        category_metrics(PageReader(FakeClient(rows), budget), "1", max_pages)


def test_complete_page_boundary_does_not_skip_a_later_qualifying_category(tmp_path):
    client = FakeClient([
        ("games/top", page([game(1), game(2)], "second")),
        ("streams", page([stream(1, 1, 6999)])),
        ("streams", page([stream(2, 2, 7000)])),
        ("games/top", page([game(3), game(4)], "third")),
        ("streams", page([stream(3, 3, 1000)])),
        ("streams", page([stream(4, 4, 500)])),
    ])
    result = collect_candidates(client_id="test", client_secret="test", client=client, now=NOW,
                                registry_path=registry(tmp_path), include_release_hints=False, category_page_size=2)
    assert [row["game_id"] for row in result["candidate_games"]] == ["2"]
    assert result["pending_verification"] == ["2"]
    assert result["top_games"] == []
    assert result["coverage"]["stop_reason"] == "whole_page_below_threshold"
    assert result["coverage"]["all_categories_enumerated"] is False
    assert len(client.calls) == 6


def test_confirmed_non_new_skipped_but_unknown_games_and_missing_igdb_are_kept(tmp_path):
    client = FakeClient([
        ("games/top", page([game(1), game(2), game(3), game(4), game(509658)])),
        ("streams", page([stream(2, 2, 7000)])),
        ("streams", page([stream(3, 3, 12000)])),
        ("streams", page([stream(4, 4, 11000)])),
    ])
    observations = {"1": observed("not_new"), "2": observed("new"),
                    "4": observed("not_new", observed_at="2026-09-27T16:00:00Z", expires_at="2026-09-28T16:00:00Z")}
    result = collect_candidates(client_id="test", client_secret="test", client=client, now=NOW,
                                registry_path=registry(tmp_path, observations), include_release_hints=False)
    assert {row["game_id"] for row in result["candidate_games"]} == {"2", "3", "4"}
    assert [row["game_id"] for row in result["top_games"]] == ["2"]
    assert set(result["pending_verification"]) == {"3", "4"}
    assert {row["game_id"] for row in result["excluded_games"]} == {"1", "509658"}
    assert [call[1]["game_id"] for call in client.calls if call[0] == "streams"] == ["2", "3", "4"]


def test_excluded_only_page_is_not_a_threshold_boundary(tmp_path):
    client = FakeClient([
        ("games/top", page([game(1)], "second")),
        ("games/top", page([game(2)])),
        ("streams", page([stream(2, 2, 9000)])),
    ])
    result = collect_candidates(client_id="test", client_secret="test", client=client, now=NOW,
                                registry_path=registry(tmp_path, {"1": observed("not_new")}), include_release_hints=False)
    assert result["candidate_games"][0]["game_id"] == "2"


def test_category_budget_cannot_silently_report_an_exhaustive_scan(tmp_path):
    client = FakeClient([("games/top", page([game(1)], "more")), ("streams", page([stream(1, 1, 9000)]))])
    with pytest.raises(IncompleteCollection, match="Category page limit"):
        collect_candidates(client_id="test", client_secret="test", client=client, now=NOW,
                           registry_path=registry(tmp_path), include_release_hints=False, max_category_pages=1)


def test_repeated_category_cursor_cannot_be_accepted_as_a_threshold_boundary(tmp_path):
    client = FakeClient([
        ("games/top", page([game(1)], "repeated")),
        ("streams", page([stream(1, 1, 9000)])),
        ("games/top", page([game(2)], "repeated")),
    ])
    with pytest.raises(IncompleteCollection, match="Repeated category cursor"):
        collect_candidates(client_id="test", client_secret="test", client=client, now=NOW,
                           registry_path=registry(tmp_path), include_release_hints=False)


def test_shipped_registry_contains_valid_direct_observations():
    load_verifications(REGISTRY_PATH)


def test_observations_expire_and_do_not_infer_from_absence():
    observations = {"1": observed("new"), "2": observed("not_new")}
    assert verification_for("1", observations, NOW)["status"] == "new"
    assert verification_for("2", observations, NOW)["status"] == "not_new"
    assert verification_for("99999999", observations, NOW)["status"] == "pending"
    later = datetime(2026, 9, 30, tzinfo=timezone.utc)
    assert verification_for("2", observations, later)["status"] == "pending"
    assert verification_for("1", observations, later)["status"] == "pending"


def test_no_undated_or_permanent_badge_cache(tmp_path):
    path = registry(tmp_path, {"1": observed("not_new", expires_at="2027-01-01T00:00:00Z")})
    with pytest.raises(ValueError, match="24 hours"):
        load_verifications(path)


def test_igdb_failure_keeps_candidates_pending_and_does_not_leak_credentials(caplog):
    class Client:
        client_id, access_token, timeout_seconds = "client-test", "secret-token-test", 1
        def _wait(self):
            pass
        @property
        def session(self):
            return self
        def post(self, *args, **kwargs):
            raise requests.ConnectionError("do not print secret-token-test")
    hints, status = release_hints(Client(), [{"igdb_id": "123"}], NOW)
    assert hints == {} and status == "unavailable_or_partial"
    assert "secret-token-test" not in caplog.text


def test_old_release_is_only_evidence_not_a_not_new_verdict():
    class Client:
        client_id, access_token, timeout_seconds = "client", "token", 1
        def _wait(self):
            pass
        @property
        def session(self):
            return self
        def post(self, *args, **kwargs):
            return self
        def raise_for_status(self):
            pass
        def json(self):
            return [{"id": 123, "first_release_date": 946684800}]
    hints, status = release_hints(Client(), [{"igdb_id": "123"}], NOW)
    assert status == "ok"
    assert hints["123"]["release_band"] == "older_release"
    assert hints["123"]["confirms_twitch_new_badge"] is False
    assert "status" not in hints["123"]
