import pytest
import requests

from collectors.twitch_live import TwitchClient

def test_twitch_oauth_posts_secret_in_body_and_sanitizes_http_error(monkeypatch):
    client = TwitchClient("client-example", "twitch-secret-test-value")
    seen = {}

    class Response:
        status_code = 401

        def raise_for_status(self):
            raise requests.HTTPError(
                "unauthorized https://id.twitch.tv/oauth2/token?"
                "client_secret=twitch-secret-test-value",
                response=self,
            )

    def post(url, **kwargs):
        seen.update(kwargs)
        assert "twitch-secret-test-value" not in url
        return Response()

    monkeypatch.setattr(client.session, "post", post)
    with pytest.raises(RuntimeError) as error:
        client.authenticate()
    assert seen["data"]["client_secret"] == "twitch-secret-test-value"
    assert "params" not in seen
    assert "HTTP 401" in str(error.value)
    assert "twitch-secret-test-value" not in str(error.value)
