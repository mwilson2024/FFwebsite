from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import parse_qs, urlsplit

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

if TYPE_CHECKING:
    from weekly_projections.history import HistoricalSeason


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


def _validated_database_url(value: str) -> str:
    """Validate a server-side PostgreSQL URL and require encrypted transport."""
    parsed = urlsplit(value)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname or not parsed.username:
        raise ValueError("WP_DATABASE_URL must be a PostgreSQL connection URL")
    query = parse_qs(parsed.query, keep_blank_values=True)
    sslmodes = [item.casefold() for item in query.get("sslmode", [])]
    if any(item not in {"require", "verify-ca", "verify-full"} for item in sslmodes):
        raise ValueError("WP_DATABASE_URL must require TLS")
    if not sslmodes:
        return value + ("&" if parsed.query else "?") + "sslmode=require"
    return value


def _postgres_connection(database_url: str):
    try:
        import psycopg
    except ImportError as error:  # pragma: no cover - exercised only by a misconfigured deployment
        raise RuntimeError("PostgreSQL storage requires the psycopg package") from error
    return psycopg.connect(
        database_url,
        connect_timeout=5,
        application_name="weekly-projections-ml",
    )


class EncryptedSessionStore:
    """Revocable server-side storage for encrypted MFL session material.

    The browser holds only an opaque random token. SQLite or PostgreSQL stores
    only its digest and authenticated ciphertext. Passwords, CSRF tokens, caches,
    and pending mutations are deliberately not accepted by this store.
    """

    def __init__(
        self,
        database_path: str | Path | None = None,
        *,
        database_url: str | None = None,
        connection_factory: Callable[[str], Any] | None = None,
    ) -> None:
        # An explicit path always selects SQLite. This keeps local development,
        # tests, and recovery tooling independent from deployment environment.
        configured_url = "" if database_path is not None else (
            database_url if database_url is not None else os.environ.get("WP_DATABASE_URL", "")
        ).strip()
        self.database_url = _validated_database_url(configured_url) if configured_url else ""
        self._connection_factory = connection_factory or _postgres_connection
        self.path: Path | None = None
        if not self.database_url:
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
    def backend(self) -> str:
        return "postgresql" if self.database_url else "sqlite"

    @property
    def lifetime_seconds(self) -> int:
        try:
            days = int(os.environ.get("WP_REMEMBER_DAYS", str(DEFAULT_REMEMBER_DAYS)))
        except ValueError:
            days = DEFAULT_REMEMBER_DAYS
        return max(1, min(MAX_REMEMBER_DAYS, days)) * 86400

    def _connect(self) -> sqlite3.Connection:
        if self.path is None:
            raise RuntimeError("SQLite is not configured")
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _connect_postgres(self):
        if not self.database_url:
            raise RuntimeError("PostgreSQL is not configured")
        return self._connection_factory(self.database_url)

    def _postgres_user_id(self, connection: Any, owner_fingerprint: str) -> Any:
        if not owner_fingerprint:
            raise ValueError("A protected owner fingerprint is required")
        row = connection.execute(
            "INSERT INTO fantasy_hq.app_user (owner_fingerprint_hash) VALUES (%s) "
            "ON CONFLICT (owner_fingerprint_hash) DO UPDATE SET last_seen_at = now() "
            "RETURNING id",
            (self._digest_bytes(owner_fingerprint),),
        ).fetchone()
        if not row:
            raise RuntimeError("PostgreSQL did not return an application user")
        return row[0]

    @staticmethod
    def _sync_postgres_account(
        connection: Any,
        user_id: Any,
        year: int,
        leagues: list[dict[str, str]],
        ranking_preference: str,
    ) -> None:
        for league in leagues:
            connection.execute(
                "INSERT INTO fantasy_hq.connected_league "
                "(user_id, season, league_id, franchise_id, league_name, api_base_url, last_selected_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, now()) "
                "ON CONFLICT (user_id, season, league_id) DO UPDATE SET "
                "franchise_id = excluded.franchise_id, league_name = excluded.league_name, "
                "api_base_url = excluded.api_base_url, last_selected_at = now()",
                (
                    user_id, int(year), str(league["id"]), str(league["franchise_id"]),
                    str(league["name"]), str(league.get("url", "")) or None,
                ),
            )
        if ranking_preference:
            connection.execute(
                "INSERT INTO fantasy_hq.user_preference (user_id, ranking_preference) VALUES (%s, %s) "
                "ON CONFLICT (user_id) DO UPDATE SET "
                "ranking_preference = excluded.ranking_preference, updated_at = now()",
                (user_id, ranking_preference[:64]),
            )

    def _initialize(self) -> None:
        if self.database_url:
            with self._connect_postgres() as connection:
                row = connection.execute(
                    "SELECT version FROM fantasy_hq.schema_migration WHERE version = %s",
                    (2,),
                ).fetchone()
                if not row:
                    raise RuntimeError("Supabase schema migration 2 is not installed")
            return
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS remembered_sessions ("
                "token_digest TEXT PRIMARY KEY, ciphertext BLOB NOT NULL, "
                "created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, last_used INTEGER NOT NULL)"
            )
            connection.execute("CREATE INDEX IF NOT EXISTS remembered_expiry ON remembered_sessions(expires_at)")
        try:
            assert self.path is not None
            self.path.chmod(0o600)
        except OSError:
            pass

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _digest_bytes(value: str) -> bytes:
        return hashlib.sha256(value.encode("utf-8")).digest()

    @staticmethod
    def _epoch(value: Any) -> int:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return int(value.timestamp())
        return int(value)

    def create(
        self,
        *,
        mfl_cookie: str,
        year: int,
        leagues: list[dict[str, str]],
        owner_fingerprint: str = "",
        ranking_preference: str = "",
    ) -> str:
        if not mfl_cookie:
            raise ValueError("Only an authenticated MFL session can be remembered")
        if self.database_url and not owner_fingerprint:
            raise ValueError("PostgreSQL remembered sessions require an owner fingerprint")
        now = int(time.time())
        expires = now + self.lifetime_seconds
        payload_value: dict[str, Any] = {
            "version": 2,
            "mfl_cookie": mfl_cookie,
            "year": int(year),
            "leagues": leagues,
            "expires_at": expires,
        }
        if owner_fingerprint:
            payload_value["owner_fingerprint"] = owner_fingerprint
        payload = json.dumps(
            payload_value,
            separators=(",", ":"),
        ).encode("utf-8")
        token = secrets.token_urlsafe(32)
        ciphertext = self.cipher.encrypt(payload)
        if self.database_url:
            with self._connect_postgres() as connection:
                user_id = self._postgres_user_id(connection, owner_fingerprint)
                self._sync_postgres_account(connection, user_id, year, leagues, ranking_preference)
                connection.execute(
                    "DELETE FROM fantasy_hq.remembered_session WHERE expires_at <= now()",
                )
                connection.execute(
                    "INSERT INTO fantasy_hq.remembered_session "
                    "(token_digest, user_id, encrypted_mfl_session, created_at, last_used_at, expires_at) "
                    "VALUES (%s, %s, %s, to_timestamp(%s), to_timestamp(%s), to_timestamp(%s))",
                    (self._digest_bytes(token), user_id, ciphertext, now, now, expires),
                )
            self.cleanup(maximum=512)
            return token
        with self._connect() as connection:
            connection.execute("DELETE FROM remembered_sessions WHERE expires_at <= ?", (now,))
            connection.execute(
                "INSERT INTO remembered_sessions VALUES (?, ?, ?, ?, ?)",
                (self._digest(token), ciphertext, now, expires, now),
            )
        # Keep a stolen or automated client from growing the durable store
        # without bound. This cleanup is intentionally outside the insert
        # transaction so it can reuse the same bounded maintenance path.
        self.cleanup(maximum=512)
        return token

    def sync_account(
        self,
        *,
        owner_fingerprint: str,
        year: int,
        leagues: list[dict[str, str]],
        ranking_preference: str,
    ) -> None:
        """Persist non-secret account metadata when PostgreSQL is configured."""
        if not self.database_url:
            return
        with self._connect_postgres() as connection:
            user_id = self._postgres_user_id(connection, owner_fingerprint)
            self._sync_postgres_account(connection, user_id, year, leagues, ranking_preference)

    def load_preferences(self, owner_fingerprint: str) -> dict[str, Any]:
        if not self.database_url or not owner_fingerprint:
            return {}
        with self._connect_postgres() as connection:
            row = connection.execute(
                "SELECT preference.theme, preference.ranking_preference, "
                "preference.default_season, preference.default_league_id, "
                "preference.selected_week, preference.briefing_alerts, "
                "preference.onboarding_complete, preference.ranking_setup_complete "
                "FROM fantasy_hq.app_user AS app_user "
                "JOIN fantasy_hq.user_preference AS preference ON preference.user_id = app_user.id "
                "WHERE app_user.owner_fingerprint_hash = %s",
                (self._digest_bytes(owner_fingerprint),),
            ).fetchone()
        if not row:
            return {}
        return {
            "theme": row[0],
            "ranking_preference": row[1],
            "default_season": row[2],
            "default_league_id": row[3],
            "selected_week": row[4],
            "briefing_alerts": bool(row[5]),
            "onboarding_complete": bool(row[6]),
            "ranking_setup_complete": bool(row[7]),
        }

    def connection_status(self) -> dict[str, Any]:
        """Return safe storage health without exposing connection details."""
        if not self.database_url:
            return {
                "backend": "SQLite",
                "configured": False,
                "connected": True,
                "schema_version": None,
            }
        with self._connect_postgres() as connection:
            row = connection.execute(
                "SELECT max(version) FROM fantasy_hq.schema_migration",
            ).fetchone()
        return {
            "backend": "Supabase PostgreSQL",
            "configured": True,
            "connected": bool(row and row[0] is not None),
            "schema_version": int(row[0]) if row and row[0] is not None else None,
        }

    def save_preferences(self, owner_fingerprint: str, **preferences: Any) -> None:
        """Persist an allow-listed set of non-secret, cross-device choices."""
        if not self.database_url:
            return
        allowed = {
            "theme": lambda value: str(value)[:32],
            "ranking_preference": lambda value: str(value)[:64],
            "default_season": int,
            "default_league_id": lambda value: str(value)[:32],
            "selected_week": int,
            "briefing_alerts": bool,
            "onboarding_complete": bool,
            "ranking_setup_complete": bool,
        }
        values = {
            key: allowed[key](value)
            for key, value in preferences.items()
            if key in allowed and value is not None
        }
        if not values:
            return
        with self._connect_postgres() as connection:
            user_id = self._postgres_user_id(connection, owner_fingerprint)
            connection.execute(
                "INSERT INTO fantasy_hq.user_preference (user_id) VALUES (%s) "
                "ON CONFLICT (user_id) DO NOTHING",
                (user_id,),
            )
            assignments = ", ".join(f"{column} = %s" for column in values)
            connection.execute(
                f"UPDATE fantasy_hq.user_preference SET {assignments}, updated_at = now() "
                "WHERE user_id = %s",
                (*values.values(), user_id),
            )

    def save_ranking_preference(self, owner_fingerprint: str, ranking_preference: str) -> None:
        self.save_preferences(owner_fingerprint, ranking_preference=ranking_preference)

    def load_watchlists(self, owner_fingerprint: str, year: int) -> dict[str, set[str]]:
        if not self.database_url or not owner_fingerprint:
            return {}
        with self._connect_postgres() as connection:
            rows = connection.execute(
                "SELECT watchlist.league_id, watchlist.player_id "
                "FROM fantasy_hq.app_user AS app_user "
                "JOIN fantasy_hq.watchlist_player AS watchlist ON watchlist.user_id = app_user.id "
                "WHERE app_user.owner_fingerprint_hash = %s AND watchlist.season = %s "
                "ORDER BY watchlist.created_at",
                (self._digest_bytes(owner_fingerprint), int(year)),
            ).fetchall()
        watchlists: dict[str, set[str]] = {}
        for league_id, player_id in rows:
            watchlists.setdefault(str(league_id), set()).add(str(player_id))
        return watchlists

    def save_watchlist_player(
        self, owner_fingerprint: str, *, year: int, league_id: str,
        player_id: str, enabled: bool,
    ) -> None:
        if not self.database_url:
            return
        with self._connect_postgres() as connection:
            user_id = self._postgres_user_id(connection, owner_fingerprint)
            if enabled:
                connection.execute(
                    "INSERT INTO fantasy_hq.watchlist_player "
                    "(user_id, season, league_id, player_id) VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (user_id, season, league_id, player_id) DO NOTHING",
                    (user_id, int(year), str(league_id), str(player_id)),
                )
            else:
                connection.execute(
                    "DELETE FROM fantasy_hq.watchlist_player "
                    "WHERE user_id = %s AND season = %s AND league_id = %s AND player_id = %s",
                    (user_id, int(year), str(league_id), str(player_id)),
                )

    def save_historical_season(
        self,
        owner_fingerprint: str,
        *,
        current_league_id: str,
        season: "HistoricalSeason",
    ) -> None:
        """Replace one normalized historical season in one database transaction."""
        if not self.database_url:
            raise RuntimeError("Historical imports require PostgreSQL storage")
        if not owner_fingerprint or not str(current_league_id).isdecimal():
            raise ValueError("A connected MFL account and league are required")
        if len(season.franchises) > 256 or len(season.matchup_teams) > 2_000:
            raise ValueError("The MFL historical season is unexpectedly large")

        with self._connect_postgres() as connection:
            installed = connection.execute(
                "SELECT version FROM fantasy_hq.schema_migration WHERE version = %s",
                (3,),
            ).fetchone()
            if not installed:
                raise RuntimeError("Supabase schema migration 3 is not installed")
            user_id = self._postgres_user_id(connection, owner_fingerprint)
            identity = (user_id, str(current_league_id), int(season.season))
            connection.execute(
                "INSERT INTO fantasy_hq.historical_season "
                "(user_id, current_league_id, season, source_league_id, league_name, "
                "start_week, end_week, regular_season_end, imported_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now()) "
                "ON CONFLICT (user_id, current_league_id, season) DO UPDATE SET "
                "source_league_id = excluded.source_league_id, "
                "league_name = excluded.league_name, start_week = excluded.start_week, "
                "end_week = excluded.end_week, regular_season_end = excluded.regular_season_end, "
                "imported_at = now()",
                (*identity, season.source_league_id, season.league_name[:200],
                 season.start_week, season.end_week, season.regular_season_end),
            )
            # Replacing child rows prevents removed MFL corrections from leaving
            # stale standings or matchups while the surrounding transaction keeps
            # the previous import intact if any insert fails.
            connection.execute(
                "DELETE FROM fantasy_hq.historical_franchise "
                "WHERE user_id = %s AND current_league_id = %s AND season = %s",
                identity,
            )
            connection.execute(
                "DELETE FROM fantasy_hq.historical_matchup_team "
                "WHERE user_id = %s AND current_league_id = %s AND season = %s",
                identity,
            )
            for row in season.franchises:
                connection.execute(
                    "INSERT INTO fantasy_hq.historical_franchise "
                    "(user_id, current_league_id, season, franchise_id, franchise_name, "
                    "division_id, standing_rank, wins, losses, ties, points_for, "
                    "points_against, victory_points) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (*identity, row.franchise_id, row.name[:200], row.division_id or None,
                     row.standing_rank, row.wins, row.losses, row.ties, row.points_for,
                     row.points_against, row.victory_points),
                )
            for row in season.matchup_teams:
                connection.execute(
                    "INSERT INTO fantasy_hq.historical_matchup_team "
                    "(user_id, current_league_id, season, week, matchup_index, "
                    "franchise_id, score) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (*identity, row.week, row.matchup_index, row.franchise_id, row.score),
                )

    def load_historical_seasons(
        self, owner_fingerprint: str, *, current_league_id: str,
    ) -> tuple[dict[str, Any], ...]:
        if not self.database_url or not owner_fingerprint:
            return ()
        with self._connect_postgres() as connection:
            rows = connection.execute(
                "SELECT season.season, season.source_league_id, season.league_name, "
                "season.imported_at, "
                "(SELECT count(*) FROM fantasy_hq.historical_franchise AS franchise "
                " WHERE franchise.user_id = season.user_id "
                " AND franchise.current_league_id = season.current_league_id "
                " AND franchise.season = season.season), "
                "(SELECT count(*) FROM (SELECT 1 FROM fantasy_hq.historical_matchup_team AS team "
                " WHERE team.user_id = season.user_id "
                " AND team.current_league_id = season.current_league_id "
                " AND team.season = season.season GROUP BY team.week, team.matchup_index) AS games) "
                "FROM fantasy_hq.app_user AS app_user "
                "JOIN fantasy_hq.historical_season AS season ON season.user_id = app_user.id "
                "WHERE app_user.owner_fingerprint_hash = %s "
                "AND season.current_league_id = %s ORDER BY season.season DESC",
                (self._digest_bytes(owner_fingerprint), str(current_league_id)),
            ).fetchall()
        return tuple({
            "season": int(row[0]),
            "source_league_id": str(row[1]),
            "league_name": str(row[2]),
            "imported_at": row[3],
            "franchises": int(row[4]),
            "matchups": int(row[5]),
        } for row in rows)

    def restore(self, token: str) -> dict[str, Any] | None:
        if not token or len(token) > 256:
            return None
        now = int(time.time())
        if self.database_url:
            digest = self._digest_bytes(token)
            with self._connect_postgres() as connection:
                row = connection.execute(
                    "SELECT encrypted_mfl_session, expires_at FROM fantasy_hq.remembered_session "
                    "WHERE token_digest = %s AND revoked_at IS NULL",
                    (digest,),
                ).fetchone()
                if not row:
                    return None
                expires = self._epoch(row[1])
                if expires <= now:
                    connection.execute(
                        "DELETE FROM fantasy_hq.remembered_session WHERE token_digest = %s",
                        (digest,),
                    )
                    return None
                try:
                    value = json.loads(self.cipher.decrypt(bytes(row[0])).decode("utf-8"))
                except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
                    connection.execute(
                        "DELETE FROM fantasy_hq.remembered_session WHERE token_digest = %s",
                        (digest,),
                    )
                    return None
                if value.get("version") not in {1, 2} or value.get("expires_at") != expires:
                    connection.execute(
                        "DELETE FROM fantasy_hq.remembered_session WHERE token_digest = %s",
                        (digest,),
                    )
                    return None
                connection.execute(
                    "UPDATE fantasy_hq.remembered_session SET last_used_at = now() WHERE token_digest = %s",
                    (digest,),
                )
                return value
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
            if value.get("version") not in {1, 2} or value.get("expires_at") != int(row[1]):
                connection.execute("DELETE FROM remembered_sessions WHERE token_digest = ?", (digest,))
                return None
            connection.execute("UPDATE remembered_sessions SET last_used = ? WHERE token_digest = ?", (now, digest))
            return value

    def revoke(self, token: str) -> None:
        if token and len(token) <= 256:
            if self.database_url:
                with self._connect_postgres() as connection:
                    connection.execute(
                        "DELETE FROM fantasy_hq.remembered_session WHERE token_digest = %s",
                        (self._digest_bytes(token),),
                    )
                return
            with self._connect() as connection:
                connection.execute("DELETE FROM remembered_sessions WHERE token_digest = ?", (self._digest(token),))

    def cleanup(self, *, maximum: int = 512) -> None:
        now = int(time.time())
        if self.database_url:
            with self._connect_postgres() as connection:
                connection.execute(
                    "DELETE FROM fantasy_hq.remembered_session WHERE expires_at <= now()",
                )
                connection.execute(
                    "DELETE FROM fantasy_hq.remembered_session WHERE token_digest IN ("
                    "SELECT token_digest FROM fantasy_hq.remembered_session "
                    "ORDER BY last_used_at DESC OFFSET %s)",
                    (max(1, maximum),),
                )
            return
        with self._connect() as connection:
            connection.execute("DELETE FROM remembered_sessions WHERE expires_at <= ?", (now,))
            connection.execute(
                "DELETE FROM remembered_sessions WHERE token_digest IN ("
                "SELECT token_digest FROM remembered_sessions ORDER BY last_used DESC LIMIT -1 OFFSET ?) ",
                (max(1, maximum),),
            )
