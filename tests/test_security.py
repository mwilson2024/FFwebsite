from __future__ import annotations

import sqlite3
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import pytest

from weekly_projections.mfl.client import MFLLeague
from weekly_projections.mfl.client import MFLRateLimitError
from weekly_projections.history import HistoricalFranchise, HistoricalMatchupTeam, HistoricalSeason
from weekly_projections.web import app as web
from weekly_projections.web.session_store import EncryptedSessionStore, _database_pool_size


class LoginClient:
    def __init__(self, config):
        self.config = config

    def login(self):
        return None

    def user_cookie(self):
        return "mfl-cookie-material"

    def account_leagues(self):
        return [MFLLeague("12345", "0001", "Secure League")]


class FakePostgresResult:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.row or []


class FakePostgresConnection:
    def __init__(self):
        self.sessions = {}
        self.preference = "combined"
        self.theme = "lions"
        self.default_league_id = "12345"
        self.selected_week = 2
        self.onboarding_complete = False
        self.ranking_setup_complete = False
        self.watchlists = set()
        self.league_themes = {}
        self.player_market_snapshots = {}
        self.provider_cache = {}
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, query, params=()):
        sql = " ".join(query.split())
        self.statements.append((sql, params))
        if sql.startswith("SELECT version FROM fantasy_hq.schema_migration"):
            return FakePostgresResult((2,))
        if sql.startswith("SELECT max(version) FROM fantasy_hq.schema_migration"):
            return FakePostgresResult((2,))
        if sql.startswith("INSERT INTO fantasy_hq.app_user"):
            return FakePostgresResult(("user-1",))
        if sql.startswith("INSERT INTO fantasy_hq.user_preference"):
            if len(params) > 1:
                self.preference = params[1]
            return FakePostgresResult()
        if sql.startswith("UPDATE fantasy_hq.user_preference"):
            value_index = 0
            for column in (
                "theme", "ranking_preference", "default_season", "default_league_id",
                "selected_week", "briefing_alerts", "onboarding_complete", "ranking_setup_complete",
            ):
                if f"{column} = %s" in sql:
                    setattr(self, column, params[value_index])
                    value_index += 1
            return FakePostgresResult()
        if sql.startswith("INSERT INTO fantasy_hq.remembered_session"):
            self.sessions[params[0]] = (
                params[2], datetime.fromtimestamp(params[5], timezone.utc),
            )
            return FakePostgresResult()
        if sql.startswith("SELECT encrypted_mfl_session"):
            return FakePostgresResult(self.sessions.get(params[0]))
        if sql.startswith("SELECT preference.theme"):
            return FakePostgresResult((
                self.theme, self.preference, 2026, self.default_league_id,
                self.selected_week, False, self.onboarding_complete, self.ranking_setup_complete,
            ))
        if sql.startswith("SELECT watchlist.league_id"):
            return FakePostgresResult(sorted(self.watchlists))
        if sql.startswith("SELECT league_theme.league_id"):
            return FakePostgresResult(sorted(self.league_themes.items()))
        if sql.startswith("INSERT INTO fantasy_hq.league_theme"):
            self.league_themes[str(params[2])] = str(params[3])
            return FakePostgresResult()
        if sql.startswith("DELETE FROM fantasy_hq.league_theme"):
            self.league_themes.clear()
            return FakePostgresResult()
        if sql.startswith("INSERT INTO fantasy_hq.watchlist_player"):
            self.watchlists.add((str(params[2]), str(params[3])))
            return FakePostgresResult()
        if sql.startswith("DELETE FROM fantasy_hq.watchlist_player"):
            self.watchlists.discard((str(params[2]), str(params[3])))
            return FakePostgresResult()
        if sql.startswith("INSERT INTO fantasy_hq.player_market_snapshot"):
            self.player_market_snapshots[(int(params[1]), str(params[2]))] = (
                json.loads(params[3]), datetime.now(timezone.utc),
            )
            return FakePostgresResult()
        if sql.startswith("SELECT snapshot.payload, snapshot.captured_at FROM"):
            return FakePostgresResult(
                self.player_market_snapshots.get((int(params[1]), str(params[2])))
            )
        if sql.startswith("INSERT INTO fantasy_hq.provider_cache"):
            now = datetime.now(timezone.utc)
            self.provider_cache[
                (params[0], int(params[1]), str(params[2]), str(params[3]))
            ] = (
                json.loads(params[4]), now,
                datetime.fromtimestamp(now.timestamp() + int(params[5]), timezone.utc),
            )
            return FakePostgresResult()
        if sql.startswith("SELECT payload, fetched_at, fresh_until FROM fantasy_hq.provider_cache"):
            return FakePostgresResult(
                self.provider_cache.get(
                    (params[0], int(params[1]), str(params[2]), str(params[3]))
                )
            )
        if sql.startswith("DELETE FROM fantasy_hq.provider_cache"):
            self.provider_cache.pop(
                (params[0], int(params[1]), str(params[2]), str(params[3])), None,
            )
            return FakePostgresResult()
        if sql.startswith("WITH expired AS"):
            return FakePostgresResult([(1,), (1,)])
        if sql.startswith("DELETE FROM fantasy_hq.remembered_session") and params and len(params) == 1:
            if isinstance(params[0], bytes):
                self.sessions.pop(params[0], None)
            return FakePostgresResult()
        return FakePostgresResult()


class HistoryPostgresConnection(FakePostgresConnection):
    def execute(self, query, params=()):
        sql = " ".join(query.split())
        if sql.startswith("SELECT version FROM fantasy_hq.schema_migration") and params == (3,):
            self.statements.append((sql, params))
            return FakePostgresResult((3,))
        if sql.startswith("SELECT season.season"):
            self.statements.append((sql, params))
            return FakePostgresResult([(
                2025, "54321", "Archive League",
                datetime(2026, 1, 1, tzinfo=timezone.utc), 2, 1,
            )])
        return super().execute(query, params)


def _store(monkeypatch, tmp_path) -> EncryptedSessionStore:
    monkeypatch.setenv("WP_SESSION_SECRET", Fernet.generate_key().decode("ascii"))
    return EncryptedSessionStore(tmp_path / "sessions.sqlite3")


def test_disabled_data_api_workaround_uses_empty_documented_schema() -> None:
    migration = (
        Path(__file__).resolve().parents[1]
        / "supabase" / "migrations" / "005_quiet_disabled_data_api.sql"
    ).read_text(encoding="utf-8")

    assert "create schema if not exists pgrst_no_exposed_schemas" in migration
    assert "alter role authenticator set pgrst.db_schemas = 'pgrst_no_exposed_schemas'" in migration
    assert "notify pgrst, 'reload config'" in migration
    assert "create schema if not exists pg_pgrst_no_exposed_schemas" not in migration


def test_player_market_snapshot_migration_is_private_and_bounded() -> None:
    migration = (
        Path(__file__).resolve().parents[1]
        / "supabase" / "migrations" / "006_player_market_snapshot.sql"
    ).read_text(encoding="utf-8")

    assert "primary key (user_id, season, league_id)" in migration
    assert "jsonb_typeof(payload) = 'object'" in migration
    assert "octet_length(payload::text) <= 5242880" in migration
    assert "enable row level security" in migration
    assert "revoke all on fantasy_hq.player_market_snapshot from anon, authenticated" in migration


def test_league_report_snapshot_migration_reuses_private_provider_cache() -> None:
    migration = (
        Path(__file__).resolve().parents[1]
        / "supabase" / "migrations" / "007_league_report_snapshots.sql"
    ).read_text(encoding="utf-8")

    assert "create table if not exists fantasy_hq.provider_cache" in migration
    assert "primary key (scope_hash, season, league_id, report_key)" in migration
    assert "jsonb_typeof(payload) = 'object'" in migration
    assert "octet_length(payload::text) <= 10485760" in migration
    assert "enable row level security" in migration
    assert "revoke all on fantasy_hq.provider_cache from anon, authenticated" in migration
    assert "create table if not exists fantasy_hq.league_report_snapshot" not in migration


def test_provider_cache_maintenance_migration_adds_cleanup_index() -> None:
    migration = (
        Path(__file__).resolve().parents[1]
        / "supabase" / "migrations" / "008_provider_cache_maintenance.sql"
    ).read_text(encoding="utf-8")

    assert "provider_cache_stale_until_idx" in migration
    assert "on fantasy_hq.provider_cache (stale_until)" in migration
    assert "values (8, 'Provider cache maintenance index')" in migration


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


def test_postgres_store_uses_private_schema_encryption_and_tls(monkeypatch):
    monkeypatch.setenv("WP_SESSION_SECRET", Fernet.generate_key().decode("ascii"))
    database = FakePostgresConnection()
    store = EncryptedSessionStore(
        database_url="postgresql://app:secret@db.example/postgres",
        connection_factory=lambda url: database,
    )
    assert store.backend == "postgresql"
    assert store.database_url.endswith("sslmode=require")

    token = store.create(
        mfl_cookie="private-cookie",
        year=2026,
        leagues=[{"id": "12345", "franchise_id": "0001", "name": "One", "url": ""}],
        owner_fingerprint="account:owner-hash",
        ranking_preference="combined",
    )
    assert store.restore(token)["mfl_cookie"] == "private-cookie"
    assert store.restore(token)["owner_fingerprint"] == "account:owner-hash"
    assert b"private-cookie" not in b"".join(value[0] for value in database.sessions.values())
    assert store.load_preferences("account:owner-hash")["ranking_preference"] == "combined"
    assert store.connection_status() == {
        "backend": "Supabase PostgreSQL",
        "configured": True,
        "connected": True,
        "schema_version": 2,
    }
    assert all("private-cookie" not in repr(params) for _, params in database.statements)

    store.save_preferences(
        "account:owner-hash", theme="pistons", default_league_id="54321",
        selected_week=7, onboarding_complete=True,
    )
    choices = store.load_preferences("account:owner-hash")
    assert choices["theme"] == "pistons"
    assert choices["default_league_id"] == "54321"
    assert choices["selected_week"] == 7
    assert choices["onboarding_complete"] is True
    store.save_league_theme(
        "account:owner-hash", year=2026, league_id="12345", theme="tigers",
    )
    assert store.load_league_themes("account:owner-hash", year=2026) == {"12345": "tigers"}
    store.clear_league_themes("account:owner-hash", year=2026)
    assert store.load_league_themes("account:owner-hash", year=2026) == {}
    store.save_watchlist_player(
        "account:owner-hash", year=2026, league_id="12345", player_id="999", enabled=True,
    )
    assert store.load_watchlists("account:owner-hash", 2026) == {"12345": {"999"}}
    store.save_watchlist_player(
        "account:owner-hash", year=2026, league_id="12345", player_id="999", enabled=False,
    )
    assert store.load_watchlists("account:owner-hash", 2026) == {}
    snapshot = {
        "version": 1,
        "week": 3,
        "roster": [],
        "players": [{"player": {"id": "123", "name": "Cached Player"}}],
    }
    store.save_player_market_snapshot(
        "account:owner-hash", year=2026, league_id="12345", payload=snapshot,
    )
    restored_snapshot = store.load_player_market_snapshot(
        "account:owner-hash", year=2026, league_id="12345",
    )
    assert restored_snapshot is not None
    assert restored_snapshot["payload"] == snapshot
    assert isinstance(restored_snapshot["captured_at"], int)
    report = {"version": 1, "report_type": "standings", "value": [{"id": "0001"}]}
    store.save_league_report_snapshot(
        "account:owner-hash", year=2026, league_id="12345",
        report_type="standings", payload=report, ttl_seconds=3600,
    )
    restored_report = store.load_league_report_snapshot(
        "account:owner-hash", year=2026, league_id="12345",
        report_type="standings", stale_seconds=86400,
    )
    assert restored_report is not None
    assert restored_report["payload"] == report
    assert restored_report["expires_at"] >= restored_report["captured_at"] + 3599
    report_select = next(
        sql for sql, _ in reversed(database.statements)
        if sql.startswith("SELECT payload, fetched_at, fresh_until FROM fantasy_hq.provider_cache")
    )
    assert "stale_until >= now()" in report_select
    assert "now() -" not in report_select
    store.delete_league_report_snapshot(
        "account:owner-hash", year=2026, league_id="12345", report_type="standings",
    )
    assert store.load_league_report_snapshot(
        "account:owner-hash", year=2026, league_id="12345", report_type="standings",
    ) is None
    assert store.prune_expired_provider_cache() == 2
    prune_sql, prune_params = next(
        (sql, params) for sql, params in reversed(database.statements)
        if sql.startswith("WITH expired AS")
    )
    assert "ORDER BY stale_until LIMIT %s" in prune_sql
    assert prune_params == (7 * 86400, 500)
    assert all("account:owner-hash" not in repr(params) for _, params in database.statements)

    store.revoke(token)
    assert store.restore(token) is None


def test_postgres_store_rejects_non_tls_urls(monkeypatch):
    monkeypatch.setenv("WP_SESSION_SECRET", Fernet.generate_key().decode("ascii"))
    with pytest.raises(ValueError, match="require TLS"):
        EncryptedSessionStore(
            database_url="postgresql://app:secret@db.example/postgres?sslmode=disable",
            connection_factory=lambda url: FakePostgresConnection(),
        )
    with pytest.raises(ValueError, match="require TLS"):
        EncryptedSessionStore(
            database_url=(
                "postgresql://app:secret@db.example/postgres"
                "?sslmode=disable&sslmode=require"
            ),
            connection_factory=lambda url: FakePostgresConnection(),
        )


def test_database_pool_size_is_small_and_bounded(monkeypatch):
    monkeypatch.delenv("WP_DATABASE_POOL_SIZE", raising=False)
    assert _database_pool_size() == 4
    monkeypatch.setenv("WP_DATABASE_POOL_SIZE", "100")
    assert _database_pool_size() == 8
    monkeypatch.setenv("WP_DATABASE_POOL_SIZE", "0")
    assert _database_pool_size() == 1
    monkeypatch.setenv("WP_DATABASE_POOL_SIZE", "invalid")
    assert _database_pool_size() == 4


def test_postgres_store_replaces_one_historical_season_transactionally(monkeypatch):
    monkeypatch.setenv("WP_SESSION_SECRET", Fernet.generate_key().decode("ascii"))
    database = HistoryPostgresConnection()
    store = EncryptedSessionStore(
        database_url="postgresql://app:secret@db.example/postgres",
        connection_factory=lambda url: database,
    )
    archive = HistoricalSeason(
        season=2025,
        source_league_id="54321",
        league_name="Archive League",
        start_week=1,
        end_week=17,
        regular_season_end=14,
        franchises=(
            HistoricalFranchise("0001", "Alpha", "01", 1, 10, 4, 0, 1600.0, 1400.0, 20.0),
            HistoricalFranchise("0002", "Beta", "01", 2, 8, 6, 0, 1500.0, 1450.0, 18.0),
        ),
        matchup_teams=(
            HistoricalMatchupTeam(1, 1, "0001", 101.0),
            HistoricalMatchupTeam(1, 1, "0002", 99.0),
        ),
    )

    store.save_historical_season(
        "account:owner-hash", current_league_id="12345", season=archive,
    )
    rows = store.load_historical_seasons(
        "account:owner-hash", current_league_id="12345",
    )

    sql = [statement for statement, _ in database.statements]
    assert any(statement.startswith("INSERT INTO fantasy_hq.historical_season") for statement in sql)
    assert any(statement.startswith("DELETE FROM fantasy_hq.historical_franchise") for statement in sql)
    assert sum(statement.startswith("INSERT INTO fantasy_hq.historical_franchise") for statement in sql) == 2
    assert sum(statement.startswith("INSERT INTO fantasy_hq.historical_matchup_team") for statement in sql) == 2
    assert rows[0]["season"] == 2025 and rows[0]["matchups"] == 1


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


def test_daily_and_game_aware_cache_windows():
    eastern = ZoneInfo("America/New_York")
    assert web._seconds_until_daily_refresh(
        datetime(2026, 9, 19, 23, 30, tzinfo=eastern)
    ) == 1800
    assert web._score_cache_ttl(
        selected_week=2, current_week=2,
        refresh_state={"active": True, "next_kickoff": None}, now=1_000,
    ) == 30
    assert web._score_cache_ttl(
        selected_week=2, current_week=2,
        refresh_state={"active": False, "next_kickoff": 4_600}, now=1_000,
    ) == 3600
    assert web._score_cache_ttl(
        selected_week=1, current_week=2,
        refresh_state={"active": False, "next_kickoff": None}, now=1_000,
    ) == 30 * 86400


def test_stable_report_cache_windows_and_status_labels(monkeypatch):
    monkeypatch.setattr(web, "_seconds_until_daily_refresh", lambda now=None: 12_345)

    assert web._report_cache_ttl("franchise-names", 300) == 7 * 86400
    assert web._report_cache_ttl("players", 300) == 86400
    assert web._report_cache_ttl("nfl-schedule:3", 300) == 7 * 86400
    assert web._report_cache_ttl("projections:3", 300) == 12 * 3600
    assert web._report_cache_ttl("reference-projections:3:combined", 300) == 12 * 3600
    assert web._report_cache_ttl("player-scores:avg", 300) == 12_345
    assert web._report_cache_ttl("player-scores:ytd", 300) == 12_345
    assert web._report_cache_ttl("live-scoring:3", 30) == 30
    assert web._report_cache_is_stable("franchise-names") is True
    assert web._report_cache_is_stable("players") is False
    assert web._report_cache_is_stable("projections:3") is True
    assert web._report_cache_is_stable("player-scores:ytd") is True
    assert web._report_cache_is_stable("live-scoring:3") is False

    details = web._cache_status_metadata("details")
    assert details["label"] == "League details"
    assert "FAAB balances" in details["detail"]
    assert web._cache_status_metadata("franchise-names")["cadence"] == "Checked weekly"
    assert web._cache_status_metadata("player-scores:avg")["cadence"] == "Checked daily"
    assert "twice daily" in web._cache_status_metadata("projections:3")["cadence"]
    assert web._cache_duration_label(604_799) == "6d 23h"
    assert web._cache_duration_label(43_200) == "12h"
    assert web._cache_duration_label(3_661) == "1h 1m"


def test_shared_stable_cache_is_account_scoped():
    web.shared_read_cache.clear()
    league = MFLLeague("12345", "0001", "One")
    first = web.BrowserSession("same-cookie", 2026, [league], "one")
    restored = web.BrowserSession("same-cookie", 2026, [league], "two")
    another = web.BrowserSession("other-cookie", 2026, [league], "three")
    calls = []
    assert web._cached_session_read(
        first, league.id, "daily-test", lambda: calls.append("first") or ["saved"],
        ttl=86400, shared=True,
    ) == ["saved"]
    assert web._cached_session_read(
        restored, league.id, "daily-test", lambda: calls.append("restored"),
        ttl=86400, shared=True,
    ) == ["saved"]
    assert web._cached_session_read(
        another, league.id, "daily-test", lambda: calls.append("other") or ["other"],
        ttl=86400, shared=True,
    ) == ["other"]
    assert calls == ["first", "other"]
