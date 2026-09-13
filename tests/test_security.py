from __future__ import annotations

import sqlite3

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from weekly_projections.mfl.client import MFLLeague
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


def test_security_headers_are_applied():
    response = TestClient(web.app, base_url="https://testserver").get("/health")
    assert response.headers["strict-transport-security"] == "max-age=31536000"
    assert "object-src 'none'" in response.headers["content-security-policy"]
    assert response.headers["permissions-policy"].startswith("camera=()")
    assert response.headers["cross-origin-opener-policy"] == "same-origin"
