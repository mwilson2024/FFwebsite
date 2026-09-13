from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


DEFAULT_REMEMBER_DAYS = 30
MAX_REMEMBER_DAYS = 30


def _private_directory() -> Path:
    configured = os.environ.get("WP_SESSION_KEY_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local:
        return (Path(local) / "WeeklyProjectionsML").resolve()
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    return ((Path(xdg) if xdg else Path.home() / ".config") / "weekly-projections-ml").resolve()


def _write_private(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(value)
    except FileExistsError:
        return
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _primary_key() -> bytes:
    configured = os.environ.get("WP_SESSION_SECRET", "").strip()
    if configured:
        key = configured.encode("ascii")
        Fernet(key)  # Validate early and fail closed.
        return key
    path = _private_directory() / "session.key"
    if not path.exists():
        _write_private(path, Fernet.generate_key())
    key = path.read_bytes().strip()
    Fernet(key)
    return key


def generate_session_secret() -> str:
    """Return a Railway-compatible key without storing or logging it."""
    return Fernet.generate_key().decode("ascii")


class EncryptedSessionStore:
    """Revocable server-side storage for encrypted MFL session material.

    The browser holds only an opaque random token. SQLite stores only its digest
    and authenticated ciphertext. Passwords, CSRF tokens, caches, and pending
    mutations are deliberately not accepted by this store.
    """

    def __init__(self, database_path: str | Path | None = None) -> None:
        configured = os.environ.get("WP_SESSION_DB", "").strip()
        project_root = Path(__file__).resolve().parents[3]
        self.path = Path(database_path or configured or project_root / ".instance" / "sessions.sqlite3").resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        primary = Fernet(_primary_key())
        previous = []
        for raw in os.environ.get("WP_SESSION_SECRET_PREVIOUS", "").split(","):
            if raw.strip():
                previous.append(Fernet(raw.strip().encode("ascii")))
        self.cipher = MultiFernet([primary, *previous])
        self._initialize()

    @property
    def lifetime_seconds(self) -> int:
        try:
            days = int(os.environ.get("WP_REMEMBER_DAYS", str(DEFAULT_REMEMBER_DAYS)))
        except ValueError:
            days = DEFAULT_REMEMBER_DAYS
        return max(1, min(MAX_REMEMBER_DAYS, days)) * 86400

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS remembered_sessions ("
                "token_digest TEXT PRIMARY KEY, ciphertext BLOB NOT NULL, "
                "created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, last_used INTEGER NOT NULL)"
            )
            connection.execute("CREATE INDEX IF NOT EXISTS remembered_expiry ON remembered_sessions(expires_at)")
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def create(self, *, mfl_cookie: str, year: int, leagues: list[dict[str, str]]) -> str:
        if not mfl_cookie:
            raise ValueError("Only an authenticated MFL session can be remembered")
        now = int(time.time())
        expires = now + self.lifetime_seconds
        payload = json.dumps(
            {"version": 1, "mfl_cookie": mfl_cookie, "year": int(year), "leagues": leagues, "expires_at": expires},
            separators=(",", ":"),
        ).encode("utf-8")
        token = secrets.token_urlsafe(32)
        with self._connect() as connection:
            connection.execute("DELETE FROM remembered_sessions WHERE expires_at <= ?", (now,))
            connection.execute(
                "INSERT INTO remembered_sessions VALUES (?, ?, ?, ?, ?)",
                (self._digest(token), self.cipher.encrypt(payload), now, expires, now),
            )
        # Keep a stolen or automated client from growing the durable store
        # without bound. This cleanup is intentionally outside the insert
        # transaction so it can reuse the same bounded maintenance path.
        self.cleanup(maximum=512)
        return token

    def restore(self, token: str) -> dict[str, Any] | None:
        if not token or len(token) > 256:
            return None
        now = int(time.time())
        digest = self._digest(token)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT ciphertext, expires_at FROM remembered_sessions WHERE token_digest = ?",
                (digest,),
            ).fetchone()
            if not row:
                return None
            if int(row[1]) <= now:
                connection.execute("DELETE FROM remembered_sessions WHERE token_digest = ?", (digest,))
                return None
            try:
                value = json.loads(self.cipher.decrypt(bytes(row[0])).decode("utf-8"))
            except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
                connection.execute("DELETE FROM remembered_sessions WHERE token_digest = ?", (digest,))
                return None
            if value.get("version") != 1 or value.get("expires_at") != int(row[1]):
                connection.execute("DELETE FROM remembered_sessions WHERE token_digest = ?", (digest,))
                return None
            connection.execute("UPDATE remembered_sessions SET last_used = ? WHERE token_digest = ?", (now, digest))
            return value

    def revoke(self, token: str) -> None:
        if token and len(token) <= 256:
            with self._connect() as connection:
                connection.execute("DELETE FROM remembered_sessions WHERE token_digest = ?", (self._digest(token),))

    def cleanup(self, *, maximum: int = 512) -> None:
        now = int(time.time())
        with self._connect() as connection:
            connection.execute("DELETE FROM remembered_sessions WHERE expires_at <= ?", (now,))
            connection.execute(
                "DELETE FROM remembered_sessions WHERE token_digest IN ("
                "SELECT token_digest FROM remembered_sessions ORDER BY last_used DESC LIMIT -1 OFFSET ?) ",
                (max(1, maximum),),
            )
