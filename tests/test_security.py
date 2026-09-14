from __future__ import annotations

import sqlite3
import time

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from weekly_projections.mfl.client import MFLLeague
from weekly_projections.mfl.client import MFLRateLimitError
from weekly_projections.web import app as web
from weekly_projections.web.session_store import EncryptedSessionStore


class LoginClient:
    def __init__(self, config):
        self.config = config

    def login(self):
        return None

    def user_cookie(self):
        return "mfl-cookie-material"

    def account_leagues(self):
        return [MFLLeague("12345", "0001", "Secure League")]


def _store(monkeypatch, tmp_path) -> EncryptedSessionStore:
    monkeypatch.setenv("WP_SESSION_SECRET", Fernet.generate_key().decode("ascii"))
    return EncryptedSessionStore(tmp_path / "sessions.sqlite3")


def test_remembered_session_is_encrypted_tamper_evident_and_revocable(monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    token = store.create(
        mfl_cookie="sensitive-mfl-cookie",
        year=2026,
        leagues=[{"id": "12345", "franchise_id": "0001", "name": "One", "url": ""}],
    )
    raw = (tmp_path / "sessions.sqlite3").read_bytes()
    assert b"sensitive-mfl-cookie" not in raw
    assert token.encode() not in raw
    assert store.restore(token)["mfl_cookie"] == "sensitive-mfl-cookie"

    with sqlite3.connect(tmp_path / "sessions.sqlite3") as connection:
        connection.execute("UPDATE remembered_sessions SET ciphertext = ?", (b"tampered",))
    assert store.restore(token) is None

    second = store.create(
        mfl_cookie="second-cookie", year=2026,
        leagues=[{"id": "12345", "franchise_id": "0001", "name": "One", "url": ""}],
    )
    store.revoke(second)
    assert store.restore(second) is None


def test_stay_signed_in_restores_fresh_app_session_without_password(monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    monkeypatch.setattr(web, "remembered_sessions", store)
    monkeypatch.setattr(web, "MFLClient", LoginClient)
    monkeypatch.setattr(web, "sessions", {})
    web.login_attempts.clear()
    client = TestClient(web.app)
    client.get("/")
    first = client.post(
        "/login",
        data={
            "username": "owner", "password": "never-store-this", "year": "2026",
            "remember_me": "1", "login_csrf": client.cookies.get("wp_login_csrf"),
        },
        follow_redirects=False,
    )
    assert first.status_code == 303
    old_session = client.cookies.get("wp_session")
    remember = client.cookies.get("wp_remember")
    assert old_session and remember
    assert b"never-store-this" not in (tmp_path / "sessions.sqlite3").read_bytes()

    web.sessions.clear()
    restored = client.get("/dashboard", follow_redirects=False)
    assert restored.status_code == 303
    assert client.cookies.get("wp_session") != old_session
    active = web.sessions[client.cookies.get("wp_session")]
    assert active.mfl_cookie == "mfl-cookie-material"
    assert active.csrf_token
    assert not active.pending_moves and not active.pending_lineups and not active.trades


def test_logout_revokes_remembered_session(monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    token = store.create(
        mfl_cookie="cookie", year=2026,
        leagues=[{"id": "12345", "franchise_id": "0001", "name": "One", "url": ""}],
    )
    monkeypatch.setattr(web, "remembered_sessions", store)
    monkeypatch.setattr(web, "sessions", {
        "active": web.BrowserSession("cookie", 2026, [MFLLeague("12345", "0001", "One")], "csrf", remember_token=token)
    })
    client = TestClient(web.app)
    client.cookies.set("wp_session", "active")
    client.cookies.set("wp_remember", token)
    response = client.post("/logout", data={"csrf_token": "csrf"}, follow_redirects=False)
    assert response.status_code == 303
    assert store.restore(token) is None
    assert "wp_session=" in response.headers["set-cookie"]
    assert response.headers["clear-site-data"] == '"cache"'


def test_login_requires_nonce_and_rejects_cross_site_origin(monkeypatch):
    monkeypatch.setattr(web, "MFLClient", LoginClient)
    monkeypatch.setattr(web, "sessions", {})
    web.login_attempts.clear()
    client = TestClient(web.app)
    assert client.post("/login", data={"username": "owner", "password": "x"}).status_code == 403
    client.get("/")
    response = client.post(
        "/login",
        data={"username": "owner", "password": "x", "login_csrf": client.cookies.get("wp_login_csrf")},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403 and not web.sessions


def test_login_accepts_railway_forwarded_same_origin(monkeypatch):
    monkeypatch.setattr(web, "MFLClient", LoginClient)
    monkeypatch.setattr(web, "sessions", {})
    web.login_attempts.clear()
    client = TestClient(web.app, base_url="https://testserver")
    client.get("/")
    response = client.post(
        "/login",
        data={
            "username": "owner", "password": "x",
            "login_csrf": client.cookies.get("wp_login_csrf"),
        },
        headers={
            "Origin": "https://fantasy.example",
            "X-Forwarded-Host": "fantasy.example",
            "Sec-Fetch-Site": "same-origin",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_get_login_redirects_to_sign_in_page():
    response = TestClient(web.app).get("/login", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_security_headers_are_applied():
    response = TestClient(web.app, base_url="https://testserver").get("/health")
    assert response.headers["strict-transport-security"] == "max-age=31536000"
    assert "object-src 'none'" in response.headers["content-security-policy"]
    assert response.headers["permissions-policy"].startswith("camera=()")
    assert response.headers["cross-origin-opener-policy"] == "same-origin"


def test_access_logging_skips_health_and_static_but_records_pages(monkeypatch):
    records = []
    monkeypatch.setattr(
        web, "log_access",
        lambda reference, request, status: records.append(
            (reference, request.url.path, status)
        ),
    )
    client = TestClient(web.app)
    assert client.get("/health").status_code == 200
    assert client.get("/").status_code == 200
    assert len(records) == 1
    assert records[0][1:] == ("/", 200)


def test_rate_limit_circuit_serves_stale_data_without_repeating_provider_call():
    current = web.BrowserSession("cookie", 2026, [MFLLeague("12345", "0001", "One")], "csrf")
    calls = []
    assert web._cached_session_read(
        current, "12345", "standings", lambda: calls.append("ok") or ["saved"], ttl=1,
    ) == ["saved"]
    key = "2026:12345:report:standings"
    current.read_cache[key] = (time.monotonic() - 2, ["saved"])

    def throttled():
        calls.append("429")
        raise MFLRateLimitError("limited", retry_after=120)

    assert web._cached_session_read(current, "12345", "standings", throttled, stale_ttl=900) == ["saved"]
    assert web._cached_session_read(current, "12345", "standings", throttled, stale_ttl=900) == ["saved"]
    assert calls == ["ok", "429"]
