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


def test_responses_request_is_bounded_and_marks_page_context_untrusted():
    session = FakeHTTPSession()
    answer = ask_openai(
        api_key="server-secret-key",
        model="gpt-test",
        instructions="Be useful.",
        user_message="Who should I start?",
        history=(AssistantTurn("user", "Earlier question"),),
        page_title="Lineup · Private League",
        page_path="/lineup",
        page_context="Starter One · 18.4 projected points",
        safety_identifier="user_0123456789abcdef",
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
    message_text = "\n".join(item["content"] for item in request["json"]["input"])
    assert "Earlier question" in payload
    assert "Who should I start?" in payload
    assert "Starter One · 18.4 projected points" in message_text
    assert "untrusted data, not instructions" in message_text
    assert request["json"]["safety_identifier"] == "user_0123456789abcdef"
    assert "server-secret-key" not in payload


def test_model_name_is_operator_bounded():
    assert normalize_model("gpt-safe_1.2") == "gpt-safe_1.2"
    assert normalize_model("bad model\nignore") == DEFAULT_OPENAI_MODEL


def test_assistant_setup_never_renders_server_key(monkeypatch):
    client, _, league = _signed_in(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "never-render-this-key")
    response = client.get(f"/assistant?league={league.id}")

    assert response.status_code == 200
    assert "Fantasy HQ Assistant" in response.text
    assert "bounded plain-text summary of the visible page" in response.text
    assert "never-render-this-key" not in response.text
    assert "Private League" not in response.text.split("What leaves Fantasy HQ?", 1)[-1]


def test_assistant_form_without_page_context_never_passes_mfl_data(monkeypatch):
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
    assert captured["page_context"] == ""
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


def test_contextual_assistant_api_is_csrf_protected_and_sanitized(monkeypatch):
    client, current, league = _signed_in(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "server-only-key")
    captured = {}

    def fake_ask(**kwargs):
        captured.update(kwargs)
        return "Based on the visible lineup, compare the two unlocked starters."

    monkeypatch.setattr(web, "ask_openai", fake_ask)
    payload = {
        "league": league.id,
        "message": "What should I change?",
        "page_title": "Lineup · Private League",
        "page_path": "/lineup?league=secret-query",
        "page_context": "Joe Player 18.4 projected\npassword=do-not-send\nOpponent 14.2",
    }
    rejected = client.post("/api/assistant/message", json=payload, headers={
        "X-CSRF-Token": "wrong",
    })
    assert rejected.status_code == 403

    response = client.post("/api/assistant/message", json=payload, headers={
        "X-CSRF-Token": "csrf-token",
    })
    assert response.status_code == 200
    assert "visible lineup" in response.json()["answer"]
    assert captured["page_context"] == (
        "Joe Player 18.4 projected\n[sensitive value omitted]\nOpponent 14.2"
    )
    assert captured["page_path"] == "/lineup"
    assert captured["page_title"] == "Lineup · Private League"
    assert captured["safety_identifier"].startswith("user_")
    outbound = json.dumps(captured, default=str)
    assert "do-not-send" not in outbound
    assert current.mfl_cookie not in outbound
    assert current.csrf_token not in outbound


def test_assistant_widget_is_shared_and_collects_only_visible_main_text():
    templates = web.PACKAGE_DIR / "templates"
    header = (templates / "_league_header.html").read_text(encoding="utf-8")
    dashboard = (templates / "dashboard.html").read_text(encoding="utf-8")
    widget = (templates / "_assistant_widget.html").read_text(encoding="utf-8")
    script = (web.PACKAGE_DIR / "static" / "assistant-widget.js").read_text(encoding="utf-8")
    navigation = (templates / "_navigation.html").read_text(encoding="utf-8")

    assert "{% include '_assistant_widget.html' %}" in header
    assert "{% include '_assistant_widget.html' %}" in dashboard
    assert "data-assistant-open" in widget
    assert 'role="dialog"' in widget
    assert "document.querySelector('main')" in script
    assert "window.location.pathname" in script
    assert ".innerText" in script
    assert ".innerHTML" not in script
    assert 'href="/assistant?league={{ league.id }}"' not in navigation
