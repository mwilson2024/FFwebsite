from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient
import pytest

from weekly_projections.mfl.client import MFLLeague
from weekly_projections.openai_assistant import (
    AssistantTurn,
    DEFAULT_OPENAI_MODEL,
    OpenAIAssistantError,
    ask_openai,
    normalize_model,
)
from weekly_projections.web import app as web


class FakeResponse:
    status_code = 200

    def json(self):
        return {
            "output": [{
                "type": "message",
                "content": [{"type": "output_text", "text": "  Check the matchup first.  "}],
            }]
        }


class FakeHTTPSession:
    def __init__(self):
        self.call = None

    def post(self, url, **kwargs):
        self.call = (url, kwargs)
        return FakeResponse()


def _signed_in(monkeypatch):
    league = MFLLeague("11111", "0001", "Private League")
    current = web.BrowserSession(
        "mfl-cookie-must-never-leave", 2026, [league], "csrf-token",
    )
    monkeypatch.setattr(web, "sessions", {"assistant-session": current})
    client = TestClient(web.app)
    client.cookies.set("wp_session", "assistant-session")
    return client, current, league


def test_responses_request_is_bounded_and_contains_only_entered_chat():
    session = FakeHTTPSession()
    answer = ask_openai(
        api_key="server-secret-key",
        model="gpt-test",
        instructions="Be useful.",
        user_message="Who should I start?",
        history=(AssistantTurn("user", "Earlier question"),),
        session=session,
    )

    assert answer == "Check the matchup first."
    url, request = session.call
    assert url == "https://api.openai.com/v1/responses"
    assert request["timeout"] == (5, 35)
    assert request["json"]["store"] is False
    assert request["json"]["max_output_tokens"] == 700
    assert request["headers"]["Authorization"] == "Bearer server-secret-key"
    payload = json.dumps(request["json"])
    assert "Earlier question" in payload
    assert "Who should I start?" in payload
    assert "server-secret-key" not in payload
    assert "mfl" not in payload.casefold()


def test_model_name_is_operator_bounded():
    assert normalize_model("gpt-safe_1.2") == "gpt-safe_1.2"
    assert normalize_model("bad model\nignore") == DEFAULT_OPENAI_MODEL


def test_assistant_setup_never_renders_server_key(monkeypatch):
    client, _, league = _signed_in(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "never-render-this-key")
    response = client.get(f"/assistant?league={league.id}")

    assert response.status_code == 200
    assert "Fantasy HQ Assistant" in response.text
    assert "Only the question you type" in response.text
    assert "never-render-this-key" not in response.text
    assert "Private League" not in response.text.split("What leaves Fantasy HQ?", 1)[-1]


def test_assistant_message_is_csrf_protected_and_never_passes_mfl_data(monkeypatch):
    client, current, league = _signed_in(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "server-only-key")
    captured = {}

    def fake_ask(**kwargs):
        captured.update(kwargs)
        return "Use the Compare players page and verify kickoff locks."

    monkeypatch.setattr(web, "ask_openai", fake_ask)
    rejected = client.post("/assistant/message", data={
        "league": league.id, "message": "Compare two players", "csrf_token": "wrong",
    })
    assert rejected.status_code == 403

    response = client.post("/assistant/message", data={
        "league": league.id, "message": "Compare two players", "csrf_token": "csrf-token",
    }, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].endswith("#latest-answer")
    outbound = json.dumps({
        key: value for key, value in captured.items() if key != "history"
    }, default=str)
    assert current.mfl_cookie not in outbound
    assert league.name not in outbound
    assert league.id not in outbound
    assert captured["user_message"] == "Compare two players"
    assert current.assistant_chats[league.id][-1].role == "assistant"

    page = client.get(f"/assistant?league={league.id}")
    assert "Use the Compare players page" in page.text
    assert "server-only-key" not in page.text


def test_assistant_chat_is_session_only_clearable_and_rate_limited(monkeypatch):
    client, current, league = _signed_in(monkeypatch)
    current.assistant_chats[league.id] = [AssistantTurn("assistant", "Saved only in memory")]

    assert client.post("/assistant/clear", data={
        "league": league.id, "csrf_token": "wrong",
    }).status_code == 403
    cleared = client.post("/assistant/clear", data={
        "league": league.id, "csrf_token": "csrf-token",
    }, follow_redirects=False)
    assert cleared.status_code == 303
    assert league.id not in current.assistant_chats

    current.assistant_request_times = [time.monotonic()] * web._ASSISTANT_RATE_LIMIT
    with pytest.raises(OpenAIAssistantError, match="Wait a minute"):
        web._check_assistant_rate_limit(current)


def test_assistant_navigation_is_available_on_league_pages():
    navigation = (
        web.PACKAGE_DIR / "templates" / "_navigation.html"
    ).read_text(encoding="utf-8")
    assert 'href="/assistant?league={{ league.id }}"' in navigation
    assert "Assistant" in navigation
