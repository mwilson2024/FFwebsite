from __future__ import annotations

import secrets
import os
import math
import statistics
import time
import hashlib
from datetime import datetime
from dataclasses import dataclass, field
from itertools import zip_longest
from pathlib import Path
from threading import BoundedSemaphore, Lock, RLock
from typing import Literal
from urllib.parse import urlencode, urlsplit

import uvicorn
import requests
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from weekly_projections.mfl.client import (
    AddDropPreview,
    MFLAvailability,
    MFLApiError,
    MFLWriteUncertainError,
    MFLClient,
    MFLConfig,
    MFLLiveScoring,
    MFLLeague,
    MFLLineupSettings,
    MFLPlayer,
    MFLRateLimitError,
)
from weekly_projections.lineup import (
    LineupRecommendation,
    assign_lineup_slots,
    lineup_slots,
    lineup_is_legal,
    recommend_lineup,
)
from weekly_projections.projection_sources import (
    ProjectionBlend,
    ProjectionSourceError,
    espn_weekly_ranks,
    projection_blend,
    stathead_weekly_scores,
)
from weekly_projections.insights import (
    ProjectionAccuracyReport,
    canonical_team,
    depth_chart_roles,
    evaluate_projection_accuracy,
    weather_for_roster,
)
from weekly_projections.live_stats import weekly_boxscore, scoring_components
from weekly_projections.recommendations import PlayerRecommendation, build_player_board
from weekly_projections.defense_streaming import rank_defense_streams, defense_waiver_pricing, TALENT_SOURCE_URL, TALENT_SOURCE_DATE
from weekly_projections.trade_engine import suggest_trades, analyze_target_trade
from weekly_projections.league_intelligence import (
    build_projected_playoff_rounds,
    build_playoff_seeds,
    build_power_rankings,
    build_recap,
    waiver_trends,
)
from weekly_projections.web.diagnostics import client_ip, initialize_log, log_access, log_error, request_context
from weekly_projections.web.session_store import EncryptedSessionStore


PACKAGE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")

_TEAM_DEFENSE_POSITIONS = {"DEF", "DST", "D/ST"}
_INDIVIDUAL_DEFENSE_POSITIONS = {
    "CB", "DB", "DE", "DL", "DT", "EDGE", "ILB", "LB", "NT", "OLB", "S", "SAF",
}
_SCORING_RULES_TTL = 7 * 86400
_SCORING_RULES_STALE_TTL = 14 * 86400


def _board_position(player: MFLPlayer) -> str:
    value = player.position.strip().upper()
    return "DEF" if value in _TEAM_DEFENSE_POSITIONS else "PK" if value == "K" else value


def _include_on_player_board(player: MFLPlayer) -> bool:
    """Keep team defense, but hide IDP records from this non-IDP player market."""
    position = _board_position(player)
    if position == "DEF":
        return True
    tokens = {token for token in position.replace("-", "/").split("/") if token}
    return not bool(tokens & _INDIVIDUAL_DEFENSE_POSITIONS)


@dataclass
class LineupPreview:
    league_id: str
    franchise_id: str
    week: int
    current_starters: tuple[MFLPlayer, ...]
    starters: tuple[MFLPlayer, ...]
    current_projection: float
    projected_total: float


@dataclass(frozen=True)
class LivePlayerView:
    player: MFLPlayer
    score: float
    status: str
    game_seconds_remaining: int
    projection: float | None = None

    @property
    def is_starter(self) -> bool:
        return self.status.casefold() in {"starter", "s"}

    @property
    def game_state(self) -> str:
        if self.game_seconds_remaining <= 0:
            return "Final"
        if self.game_seconds_remaining < 3600:
            return "Live"
        return "Upcoming"


@dataclass(frozen=True)
class LiveTeamView:
    franchise_id: str
    name: str
    score: float
    is_home: bool
    players_yet_to_play: int
    players_currently_playing: int
    players: tuple[LivePlayerView, ...]

    @property
    def starters(self):
        return tuple(player for player in self.players if player.is_starter)

    @property
    def starters_playing(self):
        return sum(0 < player.game_seconds_remaining < 3600 for player in self.starters)

    @property
    def starters_left(self):
        return sum(player.game_seconds_remaining >= 3600 for player in self.starters)

    @property
    def starter_points(self):
        return sum(player.score for player in self.starters)

    @property
    def score_difference(self):
        return round(self.score - self.starter_points, 2)


@dataclass(frozen=True)
class HeadToHeadView:
    teams: tuple[LiveTeamView, ...]
    selected_week: int | None = None
    current_week: int | None = None
    settings: MFLLineupSettings | None = None

    def rows(self, starters: bool = True):
        left = self.teams[0].players if self.teams else ()
        right = self.teams[1].players if len(self.teams) > 1 else ()
        if starters and self.settings:
            own_slots = assign_lineup_slots((item.player for item in left if item.is_starter), self.settings)
            other_slots = assign_lineup_slots((item.player for item in right if item.is_starter), self.settings)
            own = {item.player.id:item for item in left}
            other = {item.player.id:item for item in right}
            return [(a[1] and own[a[1].id], b[1] and other[b[1].id], a[0]) for a,b in zip_longest(own_slots, other_slots, fillvalue=("UNASSIGNED",None))]
        positions = {item.player.position for item in (*left, *right) if item.is_starter == starters}
        order = {position: index for index, position in enumerate(("QB", "RB", "WR", "TE", "PK", "K", "DEF", "DST", "DL", "DT", "DE", "LB", "DB", "CB", "S"))}
        if not starters:
            def bench_position(item):
                value = item.player.position.strip().upper()
                return {"K": "PK", "DST": "DEF"}.get(value, value)

            def bench_order(item):
                position = bench_position(item)
                return (order.get(position, 99), position, item.player.name.casefold(), item.player.id)

            own = sorted((item for item in left if not item.is_starter), key=bench_order)
            other = sorted((item for item in right if not item.is_starter), key=bench_order)
            paired, remaining_own = [], []
            # Reserve all same-position pairs before filling unmatched rows.
            # This maximizes exact matches without creating position-only gaps.
            for player in own:
                match = next((index for index, candidate in enumerate(other)
                              if bench_position(candidate) == bench_position(player)), None)
                if match is None:
                    remaining_own.append(player)
                else:
                    paired.append((player, other.pop(match), bench_position(player) or "BN"))
            # Different positions can share a bench row; player identities keep
            # their real positions. BN avoids implying a shared lineup slot.
            paired.extend((a, b, "BN") for a, b in zip_longest(remaining_own, other))
            return paired
        rows = []
        for position in sorted(positions, key=lambda value: (order.get(value, 99), value)):
            own = [item for item in left if item.is_starter == starters and item.player.position == position]
            other = [item for item in right if item.is_starter == starters and item.player.position == position]
            rows.extend((a, b, position) for a, b in zip_longest(own, other))
        return rows

    @property
    def forecast(self):
        if len(self.teams) != 2:
            return None
        if self.game_state == "Final":
            difference = self.teams[0].score - self.teams[1].score
            return {"percentages": (100,0) if difference > 0 else (0,100) if difference < 0 else (None,None), "final": True}
        expected, variance = [], 0.0
        for team in self.teams:
            starters = [item for item in team.players if item.is_starter]
            if not starters or (self.settings and len(starters) != self.settings.starter_count):
                return None
            total = team.score
            for item in starters:
                fraction = min(1.0, max(0, item.game_seconds_remaining / 3600))
                if self.selected_week and self.current_week and self.selected_week > self.current_week:
                    fraction = 1.0
                if not fraction:
                    continue
                if item.projection is None or not math.isfinite(item.projection):
                    return None
                total += item.projection * fraction
                variance += (max(abs(item.projection), 3.0) * .65) ** 2 * fraction
            expected.append(total)
        if variance <= 0:
            return None
        chance = 0.5 * (1 + math.erf((expected[0] - expected[1]) / math.sqrt(2 * variance)))
        percent = min(99, max(1, round(chance * 100)))
        return {"percentages": (percent,100-percent), "final": False}

    @property
    def game_state(self) -> str:
        if self.selected_week is not None and self.current_week is not None:
            if self.selected_week < self.current_week:
                return "Final"
            if self.selected_week > self.current_week:
                return "Scheduled"
        if any(team.starters_playing for team in self.teams):
            return "Live"
        if (
            self.teams
            and all(team.starters for team in self.teams)
            and all(team.starters_left == 0 for team in self.teams)
        ):
            return "Final"
        return "Scheduled"


@dataclass
class TradeDraft:
    league_id: str
    franchise_id: str
    target: str
    target_name: str
    give: tuple[MFLPlayer, ...]
    receive: tuple[MFLPlayer, ...]
    comments: str
    created_at: float = field(default_factory=time.monotonic)
    status: str = "draft"
    message: str = ""


@dataclass
class BlockDraft:
    league_id: str
    franchise_id: str
    players: tuple[MFLPlayer, ...]
    expected: dict
    wanted: str
    assets: tuple[str, ...]
    created_at: float = field(default_factory=time.monotonic)
    status: str = "draft"
    message: str = ""


@dataclass
class SideBet:
    id: str
    title: str
    participants: str
    stake: str
    status: str = "open"
    created_at: float = field(default_factory=time.time)


@dataclass
class SocialPostDraft:
    league_id: str
    franchise_id: str
    kind: str
    subject: str
    body: str
    thread_id: str = ""
    to_franchise_id: str = ""
    created_at: float = field(default_factory=time.monotonic)
    status: str = "draft"
    message: str = ""


@dataclass
class BrowserSession:
    mfl_cookie: str
    year: int
    leagues: list[MFLLeague]
    csrf_token: str
    player_catalog: dict[str, MFLPlayer] | None = None
    pending_moves: dict[str, AddDropPreview] = field(default_factory=dict)
    pending_lineups: dict[str, LineupPreview] = field(default_factory=dict)
    selected_week: int | None = None
    trades: dict[str, TradeDraft] = field(default_factory=dict)
    blocks: dict[str, BlockDraft] = field(default_factory=dict)
    expires_at: float = field(default_factory=lambda: time.monotonic() + 8 * 60 * 60)
    read_cache: dict[str, tuple[float, object]] = field(default_factory=dict)
    read_lock: RLock = field(default_factory=RLock)
    provider_cooldowns: dict[str, float] = field(default_factory=dict)
    provider_error_logs: dict[str, float] = field(default_factory=dict)
    side_bets: dict[str, list[SideBet]] = field(default_factory=dict)
    social_posts: dict[str, SocialPostDraft] = field(default_factory=dict)
    depth_snapshots: dict[str, dict[str, int]] = field(default_factory=dict)
    remember_token: str = ""
    owner_fingerprint: str = ""
    created_at: float = field(default_factory=time.monotonic)


sessions: dict[str, BrowserSession] = {}
sessions_lock = RLock()
remembered_sessions: EncryptedSessionStore | None = None
login_attempts: dict[str, list[float]] = {}
login_attempts_lock = Lock()
login_slots = BoundedSemaphore(4)
trade_send_lock = Lock()
social_send_lock = Lock()
initialize_log()
app = FastAPI(title="Weekly Projections · MFL Moves", docs_url=None, redoc_url=None)


def _allowed_hosts() -> list[str]:
    values = {"127.0.0.1", "localhost", "testserver"}
    values.update(value.strip() for value in os.environ.get("WP_ALLOWED_HOSTS", "").split(",") if value.strip())
    railway = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
    if railway:
        values.add(railway)
    return sorted(values)


app.add_middleware(TrustedHostMiddleware, allowed_hosts=_allowed_hosts())
app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")


@app.middleware("http")
async def secure_local_responses(request: Request, call_next):
    request_id = secrets.token_hex(6)
    request.state.error_reference = request_id
    token = request_context.set((request_id, request))
    caught_exception = False
    try:
        try:
            if request.method not in {"GET", "HEAD", "OPTIONS"} and not _same_origin(request):
                response = HTMLResponse("Cross-site request rejected", status_code=403)
            else:
                response = await call_next(request)
        except Exception as error:
            caught_exception = True
            log_error("unhandled_request_error", error, status=500)
            response = HTMLResponse(
                f'<h1>Something went wrong</h1><p>Error reference: {request_id}</p>'
                '<p>The error was recorded locally. <a href="/dashboard">Return to your leagues</a></p>',
                status_code=500,
            )
        route = getattr(request.scope.get("route"), "path", "")
        # Browser icon probes and random internet scans are not application
        # failures. Route-specific errors and security rejections remain logged.
        if (not caught_exception and response.status_code >= 400
                and (route or response.status_code not in {404, 405})):
            log_error("http_error", status=response.status_code)
        # Uvicorn access logs are disabled in production. Keep one structured,
        # stdout-only Railway record for user traffic without URLs or payloads.
        if request.url.path != "/health" and not request.url.path.startswith("/static/"):
            log_access(request_id, request, response.status_code)
    finally:
        request_context.reset(token)
    response.headers["X-Error-Reference"] = request_id
    # Device-local navigation preference only. It never grants league access,
    # stores credentials, or follows background widget requests.
    current = _session(request)
    restored_id = getattr(request.state, "restored_session_id", "")
    if restored_id:
        response.set_cookie(
            "wp_session", restored_id, max_age=8 * 60 * 60, path="/",
            httponly=True, samesite="strict", secure=_secure_cookies(request),
        )
    if getattr(request.state, "invalid_remember_token", False):
        response.delete_cookie("wp_remember", path="/", httponly=True, samesite="strict", secure=_secure_cookies(request))
    league_id = request.query_params.get("league")
    if (current and request.method == "GET" and response.status_code == 200
            and request.url.path in {"/home", "/lineup", "/moves", "/scores", "/trades", "/standings", "/league", "/insights"}
            and any(item.id == league_id for item in current.leagues)):
        response.set_cookie("wp_last_league", f"{current.year}:{league_id}", max_age=365*86400,
                            httponly=True, samesite="strict", secure=_secure_cookies(request))
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; font-src 'self'; "
        "img-src 'self' data: https://a.espncdn.com https://*.myfantasyleague.com; object-src 'none'; "
        "frame-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    )
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["X-Permitted-Cross-Domain-Policies"] = "none"
    if _secure_cookies(request):
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    return response


def _secure_cookies(request: Request) -> bool:
    # Explicit hosting setting also works behind Azure's HTTPS terminator.
    return os.environ.get("WP_SECURE_COOKIES", "").lower() in {"1", "true"} or request.url.scheme == "https"


def _same_origin(request: Request) -> bool:
    """Reject browser cross-site writes while tolerating trusted reverse proxies."""
    fetch_site = request.headers.get("sec-fetch-site", "").strip().casefold()
    if fetch_site == "cross-site":
        return False
    if fetch_site in {"same-origin", "none"}:
        return True
    supplied = request.headers.get("origin") or request.headers.get("referer")
    if not supplied:
        return True
    try:
        parsed = urlsplit(supplied)
        origin_host = (parsed.hostname or "").rstrip(".").casefold()
        if parsed.scheme not in {"http", "https"} or not origin_host:
            return False

        candidates = {(request.url.hostname or "").rstrip(".").casefold()}
        for header in ("host", "x-forwarded-host", "x-original-host"):
            for value in request.headers.get(header, "").split(","):
                value = value.strip()
                if not value:
                    continue
                candidate = urlsplit(f"//{value}").hostname
                if candidate:
                    candidates.add(candidate.rstrip(".").casefold())
        if origin_host in candidates:
            return True

        # Railway exposes its generated public hostname through an environment
        # variable. Custom domains are explicitly listed in WP_ALLOWED_HOSTS.
        for allowed in _allowed_hosts():
            allowed = allowed.rstrip(".").casefold()
            if allowed == "*" or origin_host == allowed:
                return True
            if allowed.startswith("*.") and origin_host.endswith(allowed[1:]):
                return True
        return False
    except ValueError:
        return False


def _cleanup_sessions() -> None:
    now = time.monotonic()
    with sessions_lock:
        expired = [key for key, value in sessions.items() if value.expires_at <= now]
        for key in expired:
            sessions.pop(key, None)
        if len(sessions) > 256:
            oldest = sorted(sessions, key=lambda key: sessions[key].created_at)[:len(sessions) - 256]
            for key in oldest:
                sessions.pop(key, None)


def _persistent_store() -> EncryptedSessionStore:
    global remembered_sessions
    if remembered_sessions is None:
        remembered_sessions = EncryptedSessionStore()
    return remembered_sessions


def _session_from_remembered(value: dict) -> BrowserSession | None:
    try:
        leagues = [
            MFLLeague(str(item["id"]), str(item["franchise_id"]), str(item["name"]), str(item.get("url", "")))
            for item in value["leagues"]
        ]
        year = int(value["year"])
        cookie = str(value["mfl_cookie"])
    except (KeyError, TypeError, ValueError):
        return None
    if not cookie or not leagues or not 2020 <= year <= 2100:
        return None
    return BrowserSession(cookie, year, leagues, secrets.token_urlsafe(32))


def _session(request: Request) -> BrowserSession | None:
    if getattr(request.state, "session_checked", False):
        return getattr(request.state, "browser_session", None)
    request.state.session_checked = True
    _cleanup_sessions()
    session_id = request.cookies.get("wp_session")
    with sessions_lock:
        current = sessions.get(session_id or "")
    if current:
        request.state.browser_session = current
        return current
    remember_token = request.cookies.get("wp_remember", "")
    if remember_token:
        try:
            restored = _session_from_remembered(_persistent_store().restore(remember_token) or {})
        except Exception as error:
            log_error("remembered_session_restore_failed", error)
            restored = None
        if restored:
            restored.remember_token = remember_token
            restored_id = secrets.token_urlsafe(32)
            with sessions_lock:
                sessions[restored_id] = restored
            request.state.restored_session_id = restored_id
            request.state.browser_session = restored
            return restored
        request.state.invalid_remember_token = True
    request.state.browser_session = None
    return current


def _login_csrf(request: Request) -> str:
    value = request.cookies.get("wp_login_csrf", "")
    return value if 32 <= len(value) <= 256 else secrets.token_urlsafe(32)


def _login_key(request: Request, username: str) -> tuple[str, str]:
    address = client_ip(request)
    account = hashlib.sha256(username.strip().casefold().encode("utf-8")).hexdigest()
    return f"ip:{address}", f"account:{account}"


def _check_login_limit(request: Request, username: str) -> tuple[str, str]:
    keys = _login_key(request, username)
    now = time.monotonic()
    with login_attempts_lock:
        for key in list(login_attempts):
            login_attempts[key] = [value for value in login_attempts[key] if now - value < 300]
            if not login_attempts[key]:
                login_attempts.pop(key, None)
        if len(login_attempts.get(keys[0], ())) >= 10 or len(login_attempts.get(keys[1], ())) >= 5:
            raise HTTPException(status_code=429, detail="Too many sign-in attempts. Wait five minutes and try again.")
    return keys


def _record_login_failure(keys: tuple[str, str]) -> None:
    now = time.monotonic()
    with login_attempts_lock:
        for key in keys:
            login_attempts.setdefault(key, []).append(now)


def _clear_login_failures(keys: tuple[str, str]) -> None:
    with login_attempts_lock:
        for key in keys:
            login_attempts.pop(key, None)


def _require_session(request: Request) -> BrowserSession:
    current = _session(request)
    if not current:
        raise HTTPException(status_code=401, detail="Connect your MFL account first")
    return current


def _league_home_url(request: Request, current: BrowserSession) -> str:
    remembered = request.cookies.get("wp_last_league", "")
    selected = next((item for item in current.leagues if remembered == f"{current.year}:{item.id}"), current.leagues[0])
    return "/home?" + urlencode({"league": selected.id})


def _league(current: BrowserSession, league_id: str) -> MFLLeague:
    match = next((league for league in current.leagues if league.id == league_id), None)
    if not match:
        raise HTTPException(status_code=404, detail="That league is not connected")
    return match


def _client(current: BrowserSession, league: MFLLeague) -> MFLClient:
    client = MFLClient(
        MFLConfig(
            year=current.year,
            league_id=league.id,
            franchise_id=league.franchise_id,
            user_cookie=current.mfl_cookie,
            base_url=league.api_base_url,
        )
    )
    client._players = current.player_catalog
    # Short-lived, session-local read cache. Mutation previews and submissions
    # intentionally bypass it and re-read MFL before any write.
    client._browser_read_cache = current.read_cache
    return client


def _cached_session_read(
    current: BrowserSession,
    league_id: str,
    label: str,
    loader,
    *,
    ttl: int = 60,
    stale_ttl: int = 900,
):
    """Coalesce display reads, serve bounded stale data, and stop 429 cascades."""
    cache_key = f"{current.year}:{league_id}:report:{label}"
    with current.read_lock:
        now = time.monotonic()
        cached = current.read_cache.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]
        stale = cached if cached and cached[0] + max(0, stale_ttl) > now else None
        cooldown_until = current.provider_cooldowns.get(league_id, 0)
        if cooldown_until > now:
            if stale:
                return stale[1]
            remaining = max(1, math.ceil(cooldown_until - now))
            raise MFLRateLimitError(
                f"MFL reads are paused for {remaining} seconds after a rate limit. "
                "Cached reports are reused where available.",
                retry_after=remaining,
            )
        try:
            value = loader()
        except MFLRateLimitError as error:
            current.provider_cooldowns[league_id] = now + error.retry_after
            if stale:
                return stale[1]
            raise
        except MFLApiError:
            if stale:
                return stale[1]
            raise
        current.provider_cooldowns.pop(league_id, None)
        current.read_cache[cache_key] = (time.monotonic() + max(1, ttl), value)
        return value


def _log_provider_error_once(
    current: BrowserSession,
    league_id: str,
    event: str,
    error: BaseException,
    *,
    window: int = 120,
) -> None:
    """Keep one actionable provider error without flooding logs per player."""
    key = f"{league_id}:{event}"
    now = time.monotonic()
    with current.read_lock:
        if current.provider_error_logs.get(key, 0) > now:
            return
        current.provider_error_logs = {
            item: expiry for item, expiry in current.provider_error_logs.items() if expiry > now
        }
        current.provider_error_logs[key] = now + max(30, window)
    log_error(event, error)


def _remember_catalog(current: BrowserSession, client: MFLClient) -> None:
    if client._players is not None:
        current.player_catalog = client._players


def _invalidate_player_board(current: BrowserSession, league_id: str) -> None:
    current.read_cache.pop(f"{current.year}:{league_id}:player-board", None)


def _standings_groups(rows: list[dict], details) -> list[dict]:
    groups = []
    assigned: set[str] = set()
    for division_id, division_name in details.divisions:
        division_rows = [
            row for row in rows
            if details.franchises.get(row["id"])
            and details.franchises[row["id"]].division_id == division_id
        ]
        if division_rows:
            groups.append({"id": division_id, "name": division_name, "rows": division_rows})
            assigned.update(row["id"] for row in division_rows)
    remaining = [row for row in rows if row["id"] not in assigned]
    if remaining or not groups:
        groups.append({"id": "", "name": "Other teams" if groups else "League standings",
                       "rows": remaining if groups else rows})
    return groups


def _activity_time(timestamp: int | None) -> str:
    if timestamp is None:
        return "Time unavailable"
    try:
        value = datetime.fromtimestamp(timestamp)
        if os.name != "nt":
            return value.strftime("%b %-d · %-I:%M %p")
        return value.strftime("%b %d · %I:%M %p").replace(" 0", " ")
    except (OverflowError, OSError, ValueError):
        return "Time unavailable"


def _league_hq(current: BrowserSession, selected: MFLLeague) -> dict:
    cache_key = f"{current.year}:{selected.id}:league-hq"
    now = time.monotonic()
    cached = current.read_cache.get(cache_key)
    if cached and cached[0] > now:
        return cached[1]
    client = _client(current, selected)
    errors: dict[str, str] = {}
    details = _cached_session_read(current, selected.id, "details", client.league_details, ttl=300)
    names = {team_id: team.name for team_id, team in details.franchises.items()}

    def read(label, loader, fallback):
        try:
            return loader()
        except (MFLApiError, ValueError, requests.RequestException) as error:
            if isinstance(error, MFLApiError):
                _log_provider_error_once(current, selected.id, f"league_hq_{label}_unavailable", error)
            else:
                log_error(f"league_hq_{label}_unavailable", error)
            errors[label] = "MFL could not load this report right now."
            return fallback

    current_week = read(
        "week", lambda: _cached_session_read(current, selected.id, "week", client.current_week), None,
    ) or details.start_week
    standings = read(
        "standings", lambda: _cached_session_read(current, selected.id, "standings", client.league_standings), [],
    )
    schedule = read(
        "schedule", lambda: _cached_session_read(current, selected.id, "schedule", client.fantasy_schedule), (),
    )
    activity = read(
        "activity",
        lambda: _cached_session_read(
            current, selected.id, "activity", lambda: client.transactions(days=21, count=200),
            ttl=90,
        ),
        (),
    )
    message_threads = read(
        "message_board",
        lambda: _cached_session_read(
            current, selected.id, "message-board", lambda: client.message_board(count=12), ttl=90,
        ),
        (),
    ) if hasattr(client, "message_board") else ()
    chat_messages = read(
        "league_chat",
        lambda: _cached_session_read(
            current, selected.id, "league-chat", lambda: client.league_chat(count=30), ttl=60,
        ),
        (),
    ) if hasattr(client, "league_chat") else ()
    own_id = selected.franchise_id.zfill(4)
    chat_messages = tuple(item for item in chat_messages if not item.to_franchise_id.strip("0")
                          or item.to_franchise_id == own_id or item.franchise_id == own_id)
    catalog = read(
        "players", lambda: _cached_session_read(current, selected.id, "players", client.players, ttl=900), {},
    )
    _remember_catalog(current, client)
    regular_schedule = tuple(game for game in schedule if game.week <= details.last_regular_season_week)
    completed_games = [
        game for game in regular_schedule
        if game.week < current_week and len(game.team_ids) >= 2
        and len(game.scores) == len(game.team_ids)
        and all(score is not None for score in game.scores)
    ]
    last_results_week = max((game.week for game in completed_games), default=None)
    last_week_results = []
    if last_results_week is not None:
        for game in completed_games:
            if game.week != last_results_week:
                continue
            high_score = max(score for score in game.scores if score is not None)
            last_week_results.append({
                "week": game.week,
                "teams": tuple({
                    "id": team_id,
                    "name": names.get(team_id, f"Team {team_id}"),
                    "logo_url": details.franchises.get(team_id).logo_url
                    if details.franchises.get(team_id) else "",
                    "score": score,
                    "winner": score == high_score and sum(
                        candidate == high_score for candidate in game.scores
                    ) == 1,
                } for team_id, score in zip(game.team_ids, game.scores)),
            })
    rankings = build_power_rankings(regular_schedule, names, current_week=current_week)
    rank_by_team = {row.franchise_id: row for row in rankings}
    recap = build_recap(regular_schedule, names, current_week=current_week)
    trends = waiver_trends(activity)
    division_by_team = {team_id: team.division_id for team_id, team in details.franchises.items()}
    seeds = build_playoff_seeds(
        standings, division_by_team, (division_id for division_id, _ in details.divisions), field_size=8,
    )
    playoff_rounds = build_projected_playoff_rounds(
        seeds,
        rank_by_team,
        first_playoff_week=details.last_regular_season_week + 1,
    )
    playoff_games = list(playoff_rounds[0].games) if playoff_rounds else []
    result = {
        "details": details,
        "teams": details.franchises,
        "names": names,
        "current_week": current_week,
        "standings": standings,
        "groups": _standings_groups(standings, details),
        "schedule": schedule,
        "last_results_week": last_results_week,
        "last_week_results": last_week_results,
        "activity": activity,
        "message_threads": message_threads,
        "chat_messages": chat_messages,
        "catalog": catalog,
        "rankings": rankings,
        "rank_by_team": rank_by_team,
        "recap": recap,
        "trends": trends[:12],
        "playoff_games": playoff_games,
        "playoff_rounds": playoff_rounds,
        "playoff_seeds": seeds,
        "errors": errors,
    }
    current.read_cache[cache_key] = (time.monotonic() + 90, result)
    return result


def _load_player_score_summaries(
    client: MFLClient,
    current: BrowserSession | None,
    through_week: int | None = None,
) -> tuple[dict[str, float], dict[str, float], dict[str, float]]:
    """Load MFL YTD/average plus a bounded recent-game scoring median.

    MFL has no median report, so at most the five most recent completed weekly
    reports are read.  Each weekly result receives a long session cache, keeping
    the extra context useful without causing a season's worth of requests.
    """
    reports: dict[str, dict[str, float]] = {"YTD": {}, "AVG": {}}
    for period in reports:
        try:
            if current is None:
                reports[period] = client.player_scores(period=period)
            else:
                reports[period] = _cached_session_read(
                    current,
                    client.config.league_id,
                    f"player-scores:{period.lower()}",
                    lambda selected_period=period: client.player_scores(period=selected_period),
                    ttl=300,
                    stale_ttl=86400,
                )
        except MFLApiError as error:
            # These summaries add context but must not take down the page when
            # MFL has not published the report or has paused reads.
            if current is not None:
                _log_provider_error_once(
                    current,
                    client.config.league_id,
                    f"player_scores_{period.lower()}_unavailable",
                    error,
                )
            else:
                log_error(f"player_scores_{period.lower()}_unavailable", error)
            reports[period] = {}
            if isinstance(error, MFLRateLimitError):
                break
        except AttributeError:
            # Lightweight synthetic clients used by internal views may not
            # implement optional season summaries.
            reports[period] = {}
    weekly: list[dict[str, float]] = []
    if through_week is not None and through_week > 1:
        first_week = max(1, through_week - 5)
        for score_week in range(first_week, through_week):
            try:
                if current is None:
                    weekly.append(client.player_scores(period=score_week))
                else:
                    weekly.append(
                        _cached_session_read(
                            current,
                            client.config.league_id,
                            f"player-scores:week-{score_week}",
                            lambda selected_week=score_week: client.player_scores(period=selected_week),
                            ttl=7 * 86400,
                            stale_ttl=30 * 86400,
                        )
                    )
            except MFLApiError as error:
                if current is not None:
                    _log_provider_error_once(
                        current,
                        client.config.league_id,
                        "player_score_median_unavailable",
                        error,
                    )
                if isinstance(error, MFLRateLimitError):
                    break
            except AttributeError:
                break
    player_ids = {player_id for scores in weekly for player_id in scores}
    medians = {
        player_id: round(statistics.median(
            scores[player_id] for scores in weekly if player_id in scores
        ), 2)
        for player_id in player_ids
    }
    client.player_ytd_scores = reports["YTD"]
    client.player_avg_scores = reports["AVG"]
    client.player_median_scores = medians
    client.player_median_window = len(weekly)
    return reports["YTD"], reports["AVG"], medians


_MFL_TEAM_ALIASES = {
    "GB": "GBP", "JAX": "JAC", "KC": "KCC", "LV": "LVR", "NE": "NEP",
    "NO": "NOS", "SF": "SFO", "TB": "TBB",
}


def _mfl_team_code(value: str) -> str:
    team = value.strip().upper()
    return _MFL_TEAM_ALIASES.get(team, team)


def _points_allowed_position(position: str) -> str:
    normalized = position.strip().upper().replace("D/ST", "DEF")
    if normalized in {"WR", "TE"}:
        return "WR+TE"
    if normalized in {"DST", "DEF"}:
        return "DEF"
    if normalized in {"K", "PK"}:
        return "PK"
    return normalized


def _opponent_strength_by_player(
    players: list[MFLPlayer],
    games: dict[str, dict],
    points_allowed: dict[str, dict[str, float]],
) -> dict[str, dict[str, object]]:
    """Rank the scheduled opponent by league-scored points allowed."""
    rank_by_position: dict[str, dict[str, int]] = {}
    totals_by_position: dict[str, int] = {}
    positions = {position for values in points_allowed.values() for position in values}
    for position in positions:
        ordered = sorted(
            (
                (team_id, values[position])
                for team_id, values in points_allowed.items()
                if position in values
            ),
            key=lambda item: (-item[1], item[0]),
        )
        rank_by_position[position] = {team_id: index for index, (team_id, _) in enumerate(ordered, 1)}
        totals_by_position[position] = len(ordered)

    result: dict[str, dict[str, object]] = {}
    for player in players:
        team = _mfl_team_code(player.team)
        game = games.get(team) or games.get(player.team.strip().upper()) or {}
        opponent = _mfl_team_code(str(game.get("opponent_team") or ""))
        if not opponent:
            display = str(game.get("opponent") or "")
            opponent = _mfl_team_code(display.replace("vs", "").replace("@", "").strip())
        position = _points_allowed_position(player.position)
        value = points_allowed.get(opponent, {}).get(position)
        rank = rank_by_position.get(position, {}).get(opponent)
        total = totals_by_position.get(position, 0)
        if value is None or rank is None or total < 2:
            continue
        if rank <= max(1, total // 3):
            label, tone = "Favorable", "easy"
        elif rank > total - max(1, total // 3):
            label, tone = "Tough", "tough"
        else:
            label, tone = "Neutral", "neutral"
        result[player.id] = {
            "opponent": opponent,
            "position": position,
            "points_allowed": round(value, 2),
            "rank": rank,
            "teams": total,
            "label": label,
            "tone": tone,
        }
    return result


def _attach_opponent_strength(
    client: MFLClient,
    current: BrowserSession | None,
    players: list[MFLPlayer],
) -> None:
    try:
        points_allowed = (
            client.points_allowed()
            if current is None
            else _cached_session_read(
                current,
                client.config.league_id,
                "points-allowed",
                client.points_allowed,
                ttl=3600,
                stale_ttl=7 * 86400,
            )
        )
        client.opponent_strength = _opponent_strength_by_player(
            players,
            getattr(client, "week_games", {}),
            points_allowed,
        )
    except (MFLApiError, AttributeError) as error:
        client.opponent_strength = {}
        if current is not None and isinstance(error, MFLApiError):
            _log_provider_error_once(
                current,
                client.config.league_id,
                "opponent_strength_unavailable",
                error,
            )


def _load_player_board(
    client: MFLClient,
    current: BrowserSession | None = None,
) -> tuple[
    int | None,
    list[MFLPlayer],
    list[PlayerRecommendation],
    ProjectionBlend,
    set[str],
]:
    cache = getattr(client, "_browser_read_cache", None)
    cache_key = f"{client.config.year}:{client.config.league_id}:player-board"
    now = time.monotonic()
    if cache is not None:
        cached = cache.get(cache_key)
        if cached and cached[0] > now:
            cached_week, _, cached_board, _, _ = cached[1]
            _load_player_score_summaries(client, current, cached_week)
            if len(cached) >= 4:
                client.week_games = cached[2]
                client.opponent_strength = cached[3]
            else:
                _attach_opponent_strength(
                    client,
                    current,
                    [item.player for item in cached_board],
                )
            return cached[1]
    def read(label, loader, *, ttl=60, stale_ttl=900):
        if current is None:
            return loader()
        return _cached_session_read(
            current, client.config.league_id, label, loader, ttl=ttl, stale_ttl=stale_ttl,
        )

    roster_ids = read(f"roster:{client.config.franchise_id}", client.roster_ids, ttl=30, stale_ttl=300)
    availability = read("free-agents", client.free_agents, ttl=30, stale_ttl=300)
    league_rosters = read("league-rosters", client.trade_rosters, ttl=60, stale_ttl=600)
    league_rosters.setdefault(client.config.franchise_id.zfill(4), set()).update(roster_ids)
    rostered_by = {
        player_id: franchise_id.zfill(4)
        for franchise_id, player_ids in league_rosters.items()
        for player_id in player_ids
    }
    details = read("details", client.league_details, ttl=300, stale_ttl=86400)
    franchise_names = {
        franchise_id.zfill(4): franchise.name
        for franchise_id, franchise in details.franchises.items()
    }
    catalog = read("players", client.players, ttl=3600, stale_ttl=86400)
    roster = [catalog.get(player_id, MFLPlayer(id=player_id, name=player_id)) for player_id in roster_ids]
    available_players = [
        catalog.get(player_id, MFLPlayer(id=player_id, name=player_id))
        for player_id in availability
        if player_id not in rostered_by
    ]
    rostered_players = [
        catalog.get(player_id, MFLPlayer(id=player_id, name=player_id))
        for player_id in rostered_by
    ]
    roster = [player for player in roster if _include_on_player_board(player)]
    available_players = [player for player in available_players if _include_on_player_board(player)]
    rostered_players = [player for player in rostered_players if _include_on_player_board(player)]
    visible_ids = {player.id for player in (*available_players, *rostered_players)}
    availability = {
        player_id: state for player_id, state in availability.items() if player_id in visible_ids
    }
    week = read("week", client.current_week, ttl=300, stale_ttl=86400)
    roster_locked: set[str] = set()
    bye_teams: set[str] = set()
    if week is not None:
        try:
            def load_week_schedule():
                loaded_kickoffs = client.nfl_team_kickoffs(week=week)
                return loaded_kickoffs, dict(getattr(client, "week_games", {}))

            kickoffs, week_games = read(
                f"nfl-schedule:{week}", load_week_schedule,
                ttl=300, stale_ttl=3600,
            )
            client.week_games = week_games
            schedule_locked = _locked_player_ids(available_players, kickoffs)
            roster_locked = _locked_player_ids(roster, kickoffs)
            # Only infer a bye when the NFL feed is clearly complete enough.
            # A partial feed must never create fake bye-week advice.
            if len(kickoffs) >= 24:
                bye_teams = {
                    player.team.upper() for player in roster
                    if player.team and player.team.upper() not in kickoffs
                }
            availability = {
                player_id: (
                    MFLAvailability(player_id, status="locked", locked=True)
                    if player_id in schedule_locked
                    else state
                )
                for player_id, state in availability.items()
            }
        except MFLApiError:
            pass
    projections: dict[str, float] = {}
    try:
        # One league-wide request is both more complete and gentler on MFL's
        # rate limit than separate free-agent and roster projection requests.
        projections = read(
            f"projections:{week}", lambda: client.projected_scores(week=week),
            ttl=300, stale_ttl=86400,
        )
    except MFLApiError:
        # The player market is still useful before weekly projections publish.
        projections = {}
    all_players = list({player.id: player for player in (*available_players, *rostered_players)}.values())
    blend = (
        projection_blend(
            all_players,
            year=client.config.year,
            week=week,
            mfl_scores=projections,
            session=client.session,
            include_espn=True,
            espn_rank_type=os.getenv("WP_ESPN_RANKING_FORMAT", "PPR"),
        )
        if week is not None
        else ProjectionBlend(
            scores=projections,
            mfl_scores=projections,
            ml_scores={},
            ml_matched=0,
        )
    )
    recommendations = build_player_board(
        available_players=available_players,
        availability=availability,
        rostered_players=rostered_players,
        rostered_by=rostered_by,
        franchise_names=franchise_names,
        own_franchise_id=client.config.franchise_id,
        own_roster=roster,
        projections=blend.scores,
        bye_teams=bye_teams,
    )
    _load_player_score_summaries(client, current, week)
    _attach_opponent_strength(client, current, all_players)
    result = (week, roster, recommendations, blend, roster_locked)
    if cache is not None:
        # This board combines several large MFL exports. A brief cache keeps
        # home widgets and the player page from immediately repeating them,
        # while submission-time ownership and lock checks remain live.
        cache[cache_key] = (
            time.monotonic() + 30,
            result,
            dict(getattr(client, "week_games", {})),
            dict(getattr(client, "opponent_strength", {})),
        )
    return result


def _load_lineup(
    client: MFLClient,
    requested_week: int | None = None,
    current: BrowserSession | None = None,
) -> tuple[int, LineupRecommendation, MFLLineupSettings, ProjectionBlend]:
    def read(label, loader, *, ttl=60, stale_ttl=900, global_feed=False):
        if current is None:
            return loader()
        return _cached_session_read(
            current,
            "mfl-global" if global_feed else client.config.league_id,
            label,
            loader,
            ttl=ttl,
            stale_ttl=stale_ttl,
        )

    week = requested_week if requested_week is not None else read(
        "week", client.current_week, ttl=300, stale_ttl=86400, global_feed=True,
    )
    if week is None:
        raise MFLApiError("MFL did not return the current lineup week")
    if not 1 <= week <= 18:
        raise MFLApiError("Choose a week from 1 through 18")
    live = None
    live_statuses: dict[str, str] = {}
    try:
        live = read(
            f"live-scoring:{week}", lambda: client.live_scoring(week=week),
            ttl=30, stale_ttl=300,
        )
        for matchup in live.matchups:
            own = next(
                (
                    franchise for franchise in matchup.franchises
                    if franchise.franchise_id.lstrip("0")
                    == client.config.franchise_id.lstrip("0")
                ),
                None,
            )
            if own is None:
                continue
            for item in own.players:
                raw_status = item.status.strip().upper().replace("-", "")
                status = {
                    "STARTER": "S",
                    "NONSTARTER": "NS",
                    "INJUREDRESERVE": "IR",
                    "TAXISQUAD": "TS",
                }.get(raw_status, raw_status)
                if status in {"S", "NS", "IR", "TS"}:
                    live_statuses[item.player_id] = status
            client.lineup_scores = {item.player_id: item.score for item in own.players}
            break
    except MFLApiError:
        # The authoritative roster/status fallback below will surface one
        # actionable error if the provider cooldown prevents it too.
        pass

    if live_statuses:
        # MFL live scoring provides the complete weekly roster plus saved
        # starter state in one league read. This materially reduces 429 risk.
        roster_ids = set(live_statuses)
        statuses = live_statuses
    else:
        roster_ids = read(
            f"roster:{client.config.franchise_id}", client.roster_ids,
            ttl=30, stale_ttl=300,
        )
        statuses = read(
            f"roster-status:{client.config.franchise_id}:{week}",
            lambda: client.player_roster_statuses(roster_ids, week=week),
            ttl=30, stale_ttl=300,
        )
        client.lineup_scores = {}
    roster = client.named_players(roster_ids)
    settings = read(
        "lineup-settings", client.lineup_settings,
        ttl=7 * 86400, stale_ttl=30 * 86400,
    )
    def load_week_schedule():
        kickoffs = client.nfl_team_kickoffs(week=week)
        return kickoffs, dict(getattr(client, "week_games", {}))

    kickoffs, week_games = read(
        f"nfl-schedule:{week}", load_week_schedule,
        ttl=300, stale_ttl=3600, global_feed=True,
    )
    # Restore display metadata when this global report came from a cache entry
    # populated while viewing another league.
    client.week_games = week_games
    client.week_schedule_complete = len(kickoffs) >= 24
    locked_player_ids = _locked_player_ids(roster, kickoffs)
    if any(statuses.get(player_id, "R") not in {"S","NS","IR","TS"} for player_id in locked_player_ids):
        raise MFLApiError("MFL has not exposed the saved lineup for a locked player. Reconnect MFL and reload this week; no lineup changes were made.")
    client.lineup_visible = any(value in {"S", "NS"} for value in statuses.values())
    try:
        all_projections = read(
            f"projections:{week}", lambda: client.projected_scores(week=week),
            ttl=300, stale_ttl=86400,
        )
        projections = {
            player_id: all_projections[player_id]
            for player_id in roster_ids if player_id in all_projections
        }
    except MFLApiError:
        projections = {}
    try:
        injuries = read(
            f"injuries:{week}", lambda: client.injuries(week=week),
            ttl=300, stale_ttl=3600, global_feed=True,
        )
    except MFLApiError:
        injuries = {}
    blend = projection_blend(
        roster,
        year=client.config.year,
        week=week,
        mfl_scores=projections,
        session=client.session,
        include_espn=True,
        espn_rank_type=os.getenv("WP_ESPN_RANKING_FORMAT", "PPR"),
    )
    _load_player_score_summaries(client, current, week)
    _attach_opponent_strength(client, current, roster)
    recommendation = recommend_lineup(
        roster=roster,
        settings=settings,
        projections=blend.scores,
        roster_statuses=statuses,
        injuries=injuries,
        locked_player_ids=locked_player_ids,
    )
    return week, recommendation, settings, blend


def _projection_accuracy(
    current: BrowserSession,
    selected: MFLLeague,
    client: MFLClient,
    *,
    week: int,
    roster_ids: set[str],
    current_blend: ProjectionBlend,
) -> ProjectionAccuracyReport:
    def load_report() -> ProjectionAccuracyReport:
        catalog = _cached_session_read(
            current, "mfl-global", "players", client.players, ttl=3600, stale_ttl=86400,
        )
        completed = tuple(range(max(1, week - 3), week))
        history = []
        for history_week in completed:
            try:
                mfl = _cached_session_read(
                    current, selected.id, f"accuracy-projections:{history_week}",
                    lambda selected_week=history_week: client.projected_scores(week=selected_week),
                    ttl=12 * 3600, stale_ttl=7 * 86400,
                )
                actual = _cached_session_read(
                    current, selected.id, f"accuracy-actual:{history_week}",
                    lambda selected_week=history_week: client.player_scores(period=selected_week),
                    ttl=12 * 3600, stale_ttl=30 * 86400,
                )
            except MFLRateLimitError:
                break
            except MFLApiError:
                continue
            try:
                ml, _ = stathead_weekly_scores(catalog.values(), year=current.year, week=history_week)
            except ProjectionSourceError:
                ml = {}
            try:
                espn = espn_weekly_ranks(
                    catalog.values(), year=current.year, week=history_week,
                    rank_type=os.getenv("WP_ESPN_RANKING_FORMAT", "PPR"),
                )
            except ProjectionSourceError:
                espn = {}
            history.append((history_week, mfl, ml, espn, actual))
        return evaluate_projection_accuracy(
            catalog,
            history,
            current_mfl=current_blend.mfl_scores,
            current_ml=current_blend.ml_scores,
            current_ids=roster_ids,
        )

    return _cached_session_read(
        current, selected.id, f"projection-accuracy:{week}", load_report,
        ttl=12 * 3600, stale_ttl=7 * 86400,
    )


def _load_insights(
    current: BrowserSession,
    selected: MFLLeague,
    *,
    include_external: bool,
    include_accuracy: bool,
) -> dict:
    client = _client(current, selected)
    week, lineup, settings, blend = _load_lineup(client, current=current)
    roster = [item.player for item in lineup.players]
    errors: dict[str, str] = {}
    depth_roles, depth_updated = {}, ""
    weather = {}
    if include_external:
        try:
            depth_roles, depth_updated, snapshot = depth_chart_roles(
                roster, year=current.year, previous=current.depth_snapshots.get(selected.id),
            )
            current.depth_snapshots[selected.id] = snapshot
        except RuntimeError as error:
            errors["depth charts"] = str(error)
            _log_provider_error_once(current, selected.id, "insights_depth_unavailable", error, window=1800)
        try:
            weather = weather_for_roster(roster, getattr(client, "week_games", {}))
        except RuntimeError as error:
            errors["weather"] = str(error)
            _log_provider_error_once(current, selected.id, "insights_weather_unavailable", error, window=900)
    accuracy = ProjectionAccuracyReport((), (), (), ())
    if include_accuracy:
        try:
            accuracy = _projection_accuracy(
                current, selected, client, week=week,
                roster_ids={player.id for player in roster}, current_blend=blend,
            )
        except (MFLApiError, ValueError, requests.RequestException) as error:
            errors["projection accuracy"] = "Projection history is temporarily unavailable."
            _log_provider_error_once(current, selected.id, "projection_accuracy_unavailable", error, window=1800)

    rows = []
    for item in lineup.players:
        team = item.player.team.upper()
        injury = item.injury
        status = injury.status if injury else "No MFL injury designation"
        severity = "danger" if injury and any(
            word in injury.status.casefold() for word in ("out", "inactive", "reserve", "suspend")
        ) else "warning" if injury else "good"
        role = depth_roles.get(item.player.id)
        outlook = weather.get(canonical_team(team))
        rows.append({
            "item": item,
            "injury_status": status,
            "injury_details": injury.details if injury else "",
            "severity": severity,
            "depth": role,
            "weather": outlook,
            "news_url": "https://www.myfantasyleague.com/" + str(current.year) + "/news_articles?" + urlencode({
                "L": selected.id, "P": item.player.id,
            }),
        })

    actions = []
    open_slots = max(0, settings.starter_count - len(lineup.current_starters))
    if open_slots:
        actions.append({
            "tone": "danger", "title": f"Fill {open_slots} open starter slot{'s' if open_slots != 1 else ''}",
            "detail": "MFL's saved lineup is short of the league starter maximum.",
            "href": f"/lineup?league={selected.id}&week={week}", "label": "Fix lineup",
        })
    changes = [item for item in lineup.players if item.action in {"START", "SIT"}]
    if changes and lineup.projected_gain > .05:
        actions.append({
            "tone": "strong", "title": f"Best-lineup suggestion adds {lineup.projected_gain:.1f} projected points",
            "detail": "Review the legal start/sit changes before confirming anything with MFL.",
            "href": f"/lineup?league={selected.id}&week={week}", "label": "Review suggestion",
        })
    for row in rows:
        if row["severity"] in {"danger", "warning"} and row["item"].recommended_start:
            actions.append({
                "tone": row["severity"], "title": f"{row['item'].player.name}: {row['injury_status']}",
                "detail": row["injury_details"] or "MFL lists an injury designation for a recommended starter.",
                "href": row["news_url"], "label": "MFL news",
            })
    for team, outlook in weather.items():
        if outlook.caution:
            player_names = ", ".join(
                item.player.name for item in lineup.players
                if canonical_team(item.player.team) == team
            )[:120]
            actions.append({
                "tone": "warning", "title": f"Weather watch: {player_names or team}",
                "detail": f"{outlook.label}; {outlook.temperature_f or '—'}°F, {outlook.precipitation_percent or 0:.0f}% precipitation, {outlook.wind_mph or 0:.0f} mph wind.",
                "href": f"/insights?league={selected.id}#weather", "label": "See forecast",
            })
    if not actions:
        actions.append({
            "tone": "good", "title": "No urgent roster alerts",
            "detail": "Your saved lineup has no visible open slots or high-priority MFL injury conflicts.",
            "href": f"/lineup?league={selected.id}&week={week}", "label": "Check lineup",
        })
    reference_by_player = {item.player_id: item for item in accuracy.references}
    reference_rows = [
        {"player": item.player, "reference": reference_by_player[item.player.id]}
        for item in lineup.players if item.player.id in reference_by_player
    ]
    return {
        "week": week, "lineup": lineup, "settings": settings, "blend": blend,
        "rows": rows, "actions": actions[:8],
        "alert_count": sum(action["tone"] in {"danger", "warning"} for action in actions[:8]),
        "depth_updated": depth_updated,
        "accuracy": accuracy, "reference_rows": reference_rows, "errors": errors,
    }


def _locked_player_ids(
    players: list[MFLPlayer] | tuple[MFLPlayer, ...],
    team_kickoffs: dict[str, int],
    *,
    now: int | None = None,
) -> set[str]:
    current_time = int(time.time()) if now is None else now
    return {
        player.id
        for player in players
        if team_kickoffs.get(player.team.upper(), current_time + 1) <= current_time
    }


def _check_locked_players_unchanged(
    *,
    selected_ids: set[str],
    statuses: dict[str, str],
    locked_ids: set[str],
) -> None:
    if any(statuses.get(player_id, "R") not in {"S", "NS", "IR", "TS"} for player_id in locked_ids):
        raise ValueError("MFL's saved starter status is unavailable for a locked player. Reload before making changes.")
    locked_starters = {
        player_id for player_id in locked_ids if statuses.get(player_id) == "S"
    }
    locked_bench = locked_ids - locked_starters
    removed = locked_starters - selected_ids
    added = locked_bench & selected_ids
    if removed or added:
        raise ValueError(
            "A player whose NFL game has kicked off cannot be moved between your starters and bench"
        )


def _load_live_scoring(
    client: MFLClient,
    current: BrowserSession | None = None,
) -> tuple[int, int, MFLLiveScoring, dict[str, str], HeadToHeadView | None]:
    return _load_live_scoring_week(client, requested_week=None, current=current)


def _load_live_scoring_week(
    client: MFLClient,
    *,
    requested_week: int | None,
    matchup_index: int | None = None,
    current: BrowserSession | None = None,
) -> tuple[int, int, MFLLiveScoring, dict[str, str], HeadToHeadView | None]:
    def read(label, loader, *, ttl=60, stale_ttl=900):
        if current is None:
            return loader()
        return _cached_session_read(
            current, client.config.league_id, label, loader, ttl=ttl, stale_ttl=stale_ttl,
        )

    current_week = read("week", client.current_week, ttl=300, stale_ttl=86400)
    if current_week is None:
        raise MFLApiError("MFL did not return the current scoring week")
    week = requested_week if requested_week is not None else current_week
    if week < 1 or week > 18:
        raise ValueError("Choose an NFL week from 1 through 18")
    live = read(
        f"live-scoring:{week}", lambda: client.live_scoring(week=week),
        ttl=30 if week == current_week else 3600,
        stale_ttl=300 if week == current_week else 86400,
    )
    names = read("franchise-names", client.franchise_names, ttl=300, stale_ttl=86400)
    catalog = read("players", client.players, ttl=3600, stale_ttl=86400)
    focused_matchup = next(
        (
            matchup
            for matchup in live.matchups
            if any(
                franchise.franchise_id == client.config.franchise_id
                for franchise in matchup.franchises
            )
        ),
        None,
    )
    if matchup_index is not None:
        if not 0 <= matchup_index < len(live.matchups):
            raise ValueError("That matchup is unavailable for this week. Choose another game.")
        focused_matchup = live.matchups[matchup_index]
    if focused_matchup is None:
        focused_matchup = live.matchups[0] if live.matchups else None
    focused_players = [
        catalog.get(item.player_id, MFLPlayer(id=item.player_id, name=item.player_id))
        for franchise in (focused_matchup.franchises if focused_matchup else ())
        for item in franchise.players
    ]
    player_ids = {player.id for player in focused_players}
    try:
        all_projections = read(
            f"projections:{week}", lambda: client.projected_scores(week=week),
            ttl=300, stale_ttl=86400,
        )
        mfl_projections = {player_id: all_projections[player_id] for player_id in player_ids if player_id in all_projections}
    except MFLApiError:
        mfl_projections = {}
    projections = projection_blend(
        focused_players,
        year=client.config.year,
        week=week,
        mfl_scores=mfl_projections,
        session=client.session,
    ).scores
    teams: list[LiveTeamView] = []
    for franchise in (focused_matchup.franchises if focused_matchup else ()):
        players = [
            LivePlayerView(
                player=catalog.get(
                    item.player_id, MFLPlayer(id=item.player_id, name=item.player_id)
                ),
                score=item.score,
                status=item.status,
                game_seconds_remaining=item.game_seconds_remaining,
                projection=projections.get(item.player_id),
            )
            for item in franchise.players
        ]
        players.sort(
            key=lambda item: (
                not item.is_starter,
                item.player.position,
                item.player.name.casefold(),
            )
        )
        teams.append(
            LiveTeamView(
                franchise_id=franchise.franchise_id,
                name=names.get(
                    franchise.franchise_id,
                    f"Franchise {franchise.franchise_id}",
                ),
                score=franchise.score,
                is_home=franchise.is_home,
                players_yet_to_play=franchise.players_yet_to_play,
                players_currently_playing=franchise.players_currently_playing,
                players=tuple(players),
            )
        )
    teams.sort(key=lambda team: team.franchise_id != client.config.franchise_id)
    try:
        settings = client.lineup_settings()
    except MFLApiError as error:
        log_error("matchup_slots_unavailable", error)
        settings = None
    head_to_head = (
        HeadToHeadView(tuple(teams), selected_week=week, current_week=current_week, settings=settings)
        if teams
        else None
    )
    return week, current_week, live, names, head_to_head


def _check_csrf(current: BrowserSession, token: str) -> None:
    if not secrets.compare_digest(current.csrf_token, token):
        raise HTTPException(status_code=403, detail="The form expired; reload and try again")


def _optional_int(value: str | int | None, label: str) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be a whole number") from error


def _stage_move(
    current: BrowserSession,
    *,
    league_id: str,
    add_id: str,
    drop_id: str,
    mode: Literal["fcfs", "waiver", "blind-bid"],
    bid: int | None,
    round_number: int | None,
    replace_existing: bool = False,
) -> tuple[str, AddDropPreview, MFLLeague]:
    league = _league(current, league_id)
    client = _client(current, league)
    preview = client.preview_add_drop(
        add=add_id,
        drop=drop_id,
        mode=mode,
        bid=bid,
        round_number=round_number,
        replace_existing=replace_existing,
    )
    week = client.current_week()
    if week is not None:
        locked = _locked_player_ids(
            [preview.add, preview.drop],
            client.nfl_team_kickoffs(week=week),
        )
        blocked = {
            player_id for player_id in locked
            if player_id == preview.drop.id or mode == "fcfs"
        }
        if blocked:
            names = ", ".join(
                player.name for player in (preview.add, preview.drop) if player.id in blocked
            )
            raise ValueError(f"Game already started; MFL has locked: {names}")
    _remember_catalog(current, client)
    pending_id = secrets.token_urlsafe(24)
    current.pending_moves[pending_id] = preview
    return pending_id, preview, league


@app.get("/health", include_in_schema=False)
def health() -> dict[str, str]:
    """Small unauthenticated liveness check for Railway and other hosts."""
    return {"status": "ok"}


@app.get("/manifest.webmanifest", include_in_schema=False)
def web_manifest() -> FileResponse:
    return FileResponse(
        PACKAGE_DIR / "static" / "manifest.webmanifest",
        media_type="application/manifest+json",
    )


@app.get("/service-worker.js", include_in_schema=False)
def service_worker() -> FileResponse:
    return FileResponse(
        PACKAGE_DIR / "static" / "service-worker.js",
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/"},
    )


@app.get("/offline", response_class=HTMLResponse, include_in_schema=False)
def offline_page(request: Request):
    return templates.TemplateResponse(request=request, name="offline.html", context={})


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    current = _session(request)
    if current:
        return RedirectResponse("/dashboard", status_code=303)
    login_csrf = _login_csrf(request)
    response = templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": None, "year": 2026, "login_csrf": login_csrf},
    )
    response.set_cookie(
        "wp_login_csrf", login_csrf, max_age=600, path="/", httponly=True,
        samesite="strict", secure=_secure_cookies(request),
    )
    if getattr(request.state, "invalid_remember_token", False):
        response.delete_cookie("wp_remember", path="/")
    return response


@app.get("/login", include_in_schema=False)
def login_page():
    """Send bookmarks and password managers back to the actual sign-in page."""
    return RedirectResponse("/", status_code=303)


@app.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    year: int = Form(2026),
    login_csrf: str = Form(""),
    remember_me: str = Form(""),
):
    expected_login_csrf = request.cookies.get("wp_login_csrf", "")
    if not expected_login_csrf or not login_csrf or not secrets.compare_digest(expected_login_csrf, login_csrf):
        raise HTTPException(status_code=403, detail="The sign-in form expired; reload and try again")
    keys = _check_login_limit(request, username)
    acquired = login_slots.acquire(blocking=False)
    if not acquired:
        raise HTTPException(status_code=503, detail="Sign-in is busy. Try again in a moment.")
    try:
        if not 2020 <= year <= 2100:
            raise ValueError("Choose a valid MFL season.")
        if not username.strip() or not password:
            raise ValueError("Enter your MFL username and password.")
        if len(username) > 254 or len(password) > 1024 or any(not character.isprintable() for character in username):
            raise ValueError("Enter valid MFL sign-in details.")
        client = MFLClient(
            MFLConfig(
                year=year,
                league_id="",
                franchise_id="",
                username=username,
                password=password,
            )
        )
        client.login()
        leagues = [league for league in client.account_leagues() if league.franchise_id != "0000"]
        if not leagues:
            raise MFLApiError(f"No owner leagues were found for the {year} season")
        session_id = secrets.token_urlsafe(32)
        current = BrowserSession(
            mfl_cookie=client.user_cookie(),
            year=year,
            leagues=leagues,
            csrf_token=secrets.token_urlsafe(32),
            owner_fingerprint=keys[1],
        )
        if remember_me == "1":
            persistent_leagues = [
                {"id": item.id, "franchise_id": item.franchise_id, "name": item.name, "url": item.url}
                for item in leagues
            ]
            current.remember_token = _persistent_store().create(
                mfl_cookie=current.mfl_cookie, year=year, leagues=persistent_leagues,
            )
        with sessions_lock:
            same_owner = sorted(
                (
                    (key, value.created_at)
                    for key, value in sessions.items()
                    if value.owner_fingerprint == current.owner_fingerprint
                ),
                key=lambda item: item[1],
            )
            # Four old sessions plus this one is the per-account ceiling.
            for old_id, _ in same_owner[:max(0, len(same_owner) - 4)]:
                sessions.pop(old_id, None)
            sessions[session_id] = current
        _cleanup_sessions()
        _clear_login_failures(keys)
    except (MFLApiError, ValueError) as error:
        _record_login_failure(keys)
        log_error("login_failed", error)
        response = templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "MFL could not verify that sign-in. Check your details and try again.", "year": year, "login_csrf": expected_login_csrf},
            status_code=401,
        )
        response.set_cookie(
            "wp_login_csrf", expected_login_csrf, max_age=600, path="/", httponly=True,
            samesite="strict", secure=_secure_cookies(request),
        )
        return response
    finally:
        login_slots.release()

    response = RedirectResponse(_league_home_url(request, sessions[session_id]), status_code=303)
    response.set_cookie(
        "wp_session",
        session_id,
        httponly=True,
        samesite="strict",
        secure=_secure_cookies(request),
        max_age=8 * 60 * 60,
        path="/",
    )
    if sessions[session_id].remember_token:
        response.set_cookie(
            "wp_remember", sessions[session_id].remember_token,
            max_age=_persistent_store().lifetime_seconds, path="/", httponly=True,
            samesite="strict", secure=_secure_cookies(request),
        )
    response.delete_cookie("wp_login_csrf", path="/", httponly=True, samesite="strict", secure=_secure_cookies(request))
    return response


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, source: str = ""):
    current = _session(request)
    if not current:
        return RedirectResponse("/", status_code=303)
    if source in {"pwa-lineup", "pwa-briefing"}:
        remembered = request.cookies.get("wp_last_league", "")
        selected = next(
            (item for item in current.leagues if remembered == f"{current.year}:{item.id}"),
            current.leagues[0],
        )
        route = "/lineup" if source == "pwa-lineup" else "/insights"
        return RedirectResponse(route + "?" + urlencode({"league": selected.id}), status_code=303)
    return RedirectResponse(_league_home_url(request, current), status_code=303)


@app.get("/home", response_class=HTMLResponse)
def league_home(request: Request, league: str, connection: str = ""):
    current = _session(request)
    if not current:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request=request, name="home.html", context={
        "session": current, "league": _league(current, league), "active_tool": "home",
        "connection": connection if connection in {"api-key-added", "invalid-key"} else ""})


@app.get("/standings", response_class=HTMLResponse)
def standings_page(request: Request, league: str):
    current = _session(request)
    if not current:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request=request, name="home.html", context={
        "session": current, "league": _league(current, league), "active_tool": "standings"})


@app.get("/insights", response_class=HTMLResponse)
def insights_page(request: Request, league: str):
    current = _require_session(request)
    selected = _league(current, league)
    try:
        context = _load_insights(
            current, selected, include_external=True, include_accuracy=True,
        )
        context.update(session=current, league=selected, active_tool="home", error=None)
    except (MFLApiError, ValueError, requests.RequestException) as error:
        if isinstance(error, MFLApiError):
            _log_provider_error_once(current, selected.id, "insights_unavailable", error)
        else:
            log_error("insights_unavailable", error)
        context = {
            "session": current, "league": selected, "active_tool": "home",
            "error": "Roster intelligence is temporarily unavailable. Existing league pages are still available.",
            "errors": {}, "rows": (), "actions": (),
            "accuracy": ProjectionAccuracyReport((), (), (), ()), "week": None,
            "depth_updated": "", "reference_rows": (), "alert_count": 0,
        }
    return templates.TemplateResponse(request=request, name="insights.html", context=context)


@app.get("/league", response_class=HTMLResponse)
def league_page(request: Request, league: str):
    current = _session(request)
    if not current:
        return RedirectResponse("/", status_code=303)
    selected = _league(current, league)
    try:
        context = _league_hq(current, selected)
    except (MFLApiError, ValueError, requests.RequestException) as error:
        if isinstance(error, MFLApiError):
            _log_provider_error_once(current, selected.id, "league_hq_unavailable", error)
        else:
            log_error("league_hq_unavailable", error)
        return templates.TemplateResponse(request=request, name="league.html", context={
            "session": current, "league": selected, "active_tool": "league",
            "error": "MFL could not load the league reports. Try again shortly.",
        })
    activity_rows = []
    for item in context["activity"]:
        teams = [context["names"].get(team_id, f"Franchise {team_id}") for team_id in item.franchise_ids]
        added = [context["catalog"].get(player_id, MFLPlayer(id=player_id, name=player_id)).name for player_id in item.adds]
        dropped = [context["catalog"].get(player_id, MFLPlayer(id=player_id, name=player_id)).name for player_id in item.drops]
        assets = [context["catalog"][player_id].name for player_id in item.assets if player_id in context["catalog"]]
        activity_rows.append({
            "item": item,
            "teams": teams,
            "added": added,
            "dropped": dropped,
            "assets": assets,
            "when": _activity_time(item.timestamp),
        })
    message_threads = [{
        "item": item,
        "author": item.author or context["names"].get(item.franchise_id, "League member"),
        "when": _activity_time(item.timestamp),
    } for item in context.get("message_threads", ())]
    chat_rows = [{
        "item": item,
        "author": context["names"].get(item.franchise_id, "League member"),
        "recipient": context["names"].get(item.to_franchise_id, "") if item.to_franchise_id.strip("0") else "",
        "when": _activity_time(item.timestamp),
    } for item in context.get("chat_messages", ())]
    context.update(
        session=current,
        league=selected,
        active_tool="league",
        error=None,
        activity_rows=activity_rows,
        message_threads=message_threads,
        chat_rows=chat_rows,
        side_bets=current.side_bets.get(selected.id, []),
    )
    return templates.TemplateResponse(request=request, name="league.html", context=context)


@app.get("/league/message-thread/{thread_id}", response_class=HTMLResponse)
def league_message_thread(request: Request, thread_id: str, league: str):
    current = _require_session(request)
    selected = _league(current, league)
    client = _client(current, selected)
    try:
        posts = _cached_session_read(
            current, selected.id, f"message-thread:{thread_id}",
            lambda: client.message_board_thread(thread_id), ttl=60, stale_ttl=300,
        )
        names = _cached_session_read(current, selected.id, "franchise-names", client.franchise_names, ttl=300)
        threads = _cached_session_read(
            current, selected.id, "message-board", lambda: client.message_board(count=12), ttl=90,
        )
    except (MFLApiError, ValueError) as error:
        log_error("message_thread_unavailable", error)
        raise HTTPException(status_code=404, detail="That MFL message-board thread is unavailable")
    subject = next((item.subject for item in threads if item.id == thread_id), "League message")
    rows = [{"item": item, "author": item.author or names.get(item.franchise_id, "League member"),
             "when": _activity_time(item.timestamp)} for item in posts]
    return templates.TemplateResponse(request=request, name="message_thread.html", context={
        "session": current, "league": selected, "active_tool": "league",
        "thread_id": thread_id, "subject": subject, "posts": rows,
    })


@app.post("/league/social/preview")
def preview_social_post(
    request: Request,
    league: str = Form(...),
    kind: str = Form(...),
    body: str = Form(...),
    csrf_token: str = Form(...),
    subject: str = Form(""),
    thread_id: str = Form(""),
    to_franchise_id: str = Form(""),
):
    current = _require_session(request)
    _check_csrf(current, csrf_token)
    selected = _league(current, league)
    kind, subject, body = kind.strip(), subject.strip(), body.strip()
    if kind not in {"board-thread", "board-reply", "chat"}:
        raise HTTPException(status_code=400, detail="Choose message board or league chat")
    if not body or len(body) > 2000 or any(ord(char) < 32 and char not in "\r\n\t" for char in body):
        raise HTTPException(status_code=400, detail="Enter a message of 2,000 characters or fewer")
    if kind == "board-thread" and (not subject or len(subject) > 120):
        raise HTTPException(status_code=400, detail="Enter a subject of 120 characters or fewer")
    if kind == "board-reply" and (not thread_id or len(thread_id) > 80
            or not all(char.isalnum() or char in "-_" for char in thread_id)):
        raise HTTPException(status_code=400, detail="Choose a valid message-board thread")
    to_franchise_id = to_franchise_id.zfill(4) if to_franchise_id else ""
    if kind == "chat" and to_franchise_id:
        details = _cached_session_read(current, selected.id, "details", _client(current, selected).league_details, ttl=300)
        if to_franchise_id not in details.franchises:
            raise HTTPException(status_code=400, detail="Choose a franchise in this league")
    pending_id = secrets.token_urlsafe(12)
    current.social_posts[pending_id] = SocialPostDraft(
        selected.id, selected.franchise_id.zfill(4), kind, subject, body, thread_id, to_franchise_id,
    )
    return RedirectResponse(f"/league/social/review/{pending_id}", status_code=303)


@app.get("/league/social/review/{pending_id}", response_class=HTMLResponse)
def review_social_post(request: Request, pending_id: str):
    current = _require_session(request)
    draft = current.social_posts.get(pending_id)
    if not draft or time.monotonic() - draft.created_at > 1200:
        current.social_posts.pop(pending_id, None)
        raise HTTPException(status_code=404, detail="That message draft expired")
    selected = _league(current, draft.league_id)
    return templates.TemplateResponse(request=request, name="social_review.html", context={
        "session": current, "league": selected, "active_tool": "league",
        "draft": draft, "pending_id": pending_id,
    })


@app.get("/league/social/send/{pending_id}")
def send_social_post_get(request: Request, pending_id: str):
    return RedirectResponse(f"/league/social/review/{pending_id}", status_code=303)


@app.post("/league/social/send/{pending_id}", response_class=HTMLResponse)
def send_social_post(request: Request, pending_id: str, csrf_token: str = Form(...)):
    current = _require_session(request)
    _check_csrf(current, csrf_token)
    draft = current.social_posts.get(pending_id)
    if not draft or draft.status != "draft" or time.monotonic() - draft.created_at > 1200:
        raise HTTPException(status_code=404, detail="That message draft is unavailable")
    selected = _league(current, draft.league_id)
    if selected.franchise_id.zfill(4) != draft.franchise_id:
        raise HTTPException(status_code=403, detail="This message belongs to another franchise")
    client = _client(current, selected)
    with social_send_lock:
        if draft.status != "draft":
            raise HTTPException(status_code=409, detail="This message has already been submitted")
        draft.status = "sending"
        try:
            if draft.kind == "chat":
                client.post_chat(body=draft.body, to_franchise_id=draft.to_franchise_id)
            else:
                client.post_message_board(subject=draft.subject, body=draft.body, thread_id=draft.thread_id)
        except MFLWriteUncertainError as error:
            draft.status, draft.message = "uncertain", str(error)
            log_error("league_social_post_uncertain", error)
        except MFLApiError as error:
            draft.status, draft.message = "draft", str(error)
            log_error("league_social_post_failed", error)
        else:
            draft.status, draft.message = "sent", "MFL accepted the message."
            for key in tuple(current.read_cache):
                if any(token in key for token in ("message-board", "message-thread:", "league-chat", "league-hq")):
                    current.read_cache.pop(key, None)
    return templates.TemplateResponse(request=request, name="social_review.html", context={
        "session": current, "league": selected, "active_tool": "league",
        "draft": draft, "pending_id": pending_id,
    })


@app.get("/manager/{franchise_id}", response_class=HTMLResponse)
def manager_page(request: Request, franchise_id: str, league: str):
    current = _session(request)
    if not current:
        return RedirectResponse("/", status_code=303)
    selected = _league(current, league)
    franchise_id = franchise_id.zfill(4)
    context = _league_hq(current, selected)
    team = context["teams"].get(franchise_id)
    if not team:
        raise HTTPException(status_code=404, detail="That manager is not in this league")
    context.update(session=current, league=selected, active_tool="league", team=team,
                   ranking=context["rank_by_team"].get(franchise_id))
    return templates.TemplateResponse(request=request, name="manager.html", context=context)


@app.post("/league/side-bets")
def create_side_bet(
    request: Request,
    league: str = Form(...),
    title: str = Form(...),
    participants: str = Form(""),
    stake: str = Form(""),
    csrf_token: str = Form(...),
):
    current = _require_session(request)
    _check_csrf(current, csrf_token)
    selected = _league(current, league)
    title, participants, stake = title.strip(), participants.strip(), stake.strip()
    if not title or len(title) > 120 or len(participants) > 120 or len(stake) > 80:
        raise HTTPException(status_code=400, detail="Enter a short prop, participants, and stake")
    bets = current.side_bets.setdefault(selected.id, [])
    if len(bets) >= 100:
        raise HTTPException(status_code=400, detail="This session has reached 100 side bets")
    bets.insert(0, SideBet(secrets.token_urlsafe(10), title, participants, stake))
    return RedirectResponse(f"/league?league={selected.id}#side-bets", status_code=303)


@app.post("/league/side-bets/{bet_id}/settle")
def settle_side_bet(
    request: Request,
    bet_id: str,
    league: str = Form(...),
    csrf_token: str = Form(...),
):
    current = _require_session(request)
    _check_csrf(current, csrf_token)
    selected = _league(current, league)
    bet = next((item for item in current.side_bets.get(selected.id, []) if item.id == bet_id), None)
    if not bet:
        raise HTTPException(status_code=404, detail="That side bet is no longer available")
    bet.status = "settled"
    return RedirectResponse(f"/league?league={selected.id}#side-bets", status_code=303)


@app.get("/hub/{section}", response_class=HTMLResponse)
def hub_section(request: Request, section: str, league: str, target: str = "", wanted: str = "", package_size: int = 2):
    current = _require_session(request)
    selected = _league(current, league)
    if section not in {"briefing", "matchup", "pickups", "standings", "block", "ideas"}:
        raise HTTPException(status_code=404)
    client = _client(current, selected)
    context = {"session": current, "league": selected, "section": section, "error": None}
    try:
        if section == "briefing":
            briefing = _load_insights(
                current, selected, include_external=False, include_accuracy=False,
            )
            context.update(week=briefing["week"], actions=briefing["actions"])
        elif section == "matchup":
            week, _, _, _, matchup = _load_live_scoring_week(client, requested_week=None, current=current)
            own = selected.franchise_id.zfill(4)
            if not matchup or not any(team.franchise_id.zfill(4) == own for team in matchup.teams):
                raise ValueError("MFL has no current matchup for your team.")
            context.update(week=week, matchup=matchup, own=own)
        elif section == "pickups":
            week, _, recommendations, _, _ = _load_player_board(client, current)
            picks = [item for item in recommendations if item.availability.claimable
                     and item.projection is not None and item.roster_delta is not None and item.roster_delta > 0][:5]
            context.update(week=week, picks=picks)
        elif section == "standings":
            rows = _cached_session_read(current, selected.id, "standings", client.league_standings)
            details = _cached_session_read(current, selected.id, "details", client.league_details, ttl=300)
            context.update(
                rows=rows,
                groups=_standings_groups(rows, details),
                teams=details.franchises,
                has_divisions=bool(details.divisions),
            )
        elif section == "block":
            block = _cached_session_read(
                current, selected.id, "trade-block", client.trade_block, ttl=60, stale_ttl=900,
            )
            catalog = _cached_session_read(
                current, selected.id, "players", client.players, ttl=3600, stale_ttl=86400,
            )
            own_block = block.get(selected.franchise_id.zfill(4), {"assets": (), "wanted": ""})
            league_rosters = _cached_session_read(
                current, selected.id, "league-rosters", client.trade_rosters, ttl=60, stale_ttl=600,
            )
            roster = league_rosters.get(selected.franchise_id.zfill(4), set())
            names = _cached_session_read(
                current, selected.id, "franchise-names", client.franchise_names, ttl=300, stale_ttl=86400,
            )
            context.update(block=block, catalog=catalog, names=names, own_block=own_block,
                           players=sorted((catalog[p] for p in roster if p in catalog), key=lambda p:(p.position, p.name)))
        else:
            week = _cached_session_read(
                current, selected.id, "week", client.current_week, ttl=300, stale_ttl=86400,
            )
            if week is None:
                raise ValueError("The current projection week is unavailable.")
            own = selected.franchise_id.zfill(4)
            target = target.zfill(4) if target else ""
            if wanted:
                if not target or target == own or not wanted.isdecimal():
                    raise ValueError("Choose a player from another team to build offers.")
                rosters = {
                    own: _cached_session_read(
                        current, selected.id, f"roster:{own}", lambda: client.franchise_roster(own),
                        ttl=30, stale_ttl=300,
                    ),
                    target: _cached_session_read(
                        current, selected.id, f"roster:{target}", lambda: client.franchise_roster(target),
                        ttl=30, stale_ttl=300,
                    ),
                }
            else:
                rosters = _cached_session_read(
                    current, selected.id, "league-rosters", client.trade_rosters, ttl=60, stale_ttl=600,
                )
            catalog = _cached_session_read(
                current, selected.id, "players", client.players, ttl=3600, stale_ttl=86400,
            )
            ids = set().union(*rosters.values()) if rosters else set()
            all_projections = _cached_session_read(
                current, selected.id, f"projections:{week}",
                lambda: client.projected_scores(week=week), ttl=300, stale_ttl=86400,
            )
            projections = {player_id: all_projections[player_id] for player_id in ids if player_id in all_projections}
            settings = _cached_session_read(
                current, selected.id, "lineup-settings", client.lineup_settings, ttl=300, stale_ttl=86400,
            )
            details = _cached_session_read(
                current, selected.id, "details", client.league_details, ttl=300, stale_ttl=86400,
            )
            ros_weeks = max(0, details.end_week - week + 1)
            if wanted:
                analysis = analyze_target_trade(own, target, wanted, rosters, catalog, projections, settings, package_size=package_size)
                context.update(analysis=analysis, target=target, ros_weeks=ros_weeks)
            else:
                context["ideas"] = suggest_trades(own, rosters, catalog, projections, settings)
            names = _cached_session_read(
                current, selected.id, "franchise-names", client.franchise_names, ttl=300, stale_ttl=86400,
            )
            context.update(week=week, names=names, ros_weeks=ros_weeks)
        _remember_catalog(current, client)
    except (MFLApiError, ValueError, requests.RequestException) as exc:
        if isinstance(exc, MFLApiError):
            _log_provider_error_once(current, selected.id, f"hub_{section}_unavailable", exc)
        else:
            log_error("hub_section_unavailable", exc)
        context["error"] = str(exc) if isinstance(exc, ValueError) else "MFL could not load this section. Try again shortly."
    return templates.TemplateResponse(request=request, name="_hub_section.html", context=context)


@app.get("/moves", response_class=HTMLResponse)
def moves(request: Request, league: str, q: str = "", error: str = ""):
    current = _session(request)
    if not current:
        return RedirectResponse("/", status_code=303)
    selected = _league(current, league)
    client = _client(current, selected)
    api_error: str | None = error[:300] or None
    roster: list[MFLPlayer] = []
    recommendations: list[PlayerRecommendation] = []
    blend = ProjectionBlend(scores={}, mfl_scores={}, ml_scores={}, ml_matched=0)
    roster_locked: set[str] = set()
    week: int | None = None
    query = q.strip()[:80]
    try:
        week, roster, recommendations, blend, roster_locked = _load_player_board(client, current)
        _remember_catalog(current, client)
    except MFLApiError as caught:
        log_error("player_board_failed", caught)
        api_error = str(caught)
    positions = sorted(
        {_board_position(item.player) for item in recommendations if item.player.position},
        key=lambda value: (value not in {"QB", "RB", "WR", "TE", "PK", "DEF"}, value),
    )
    nfl_teams = sorted({item.player.team for item in recommendations if item.player.team})
    defense_streams = rank_defense_streams(
        recommendations,
        year=current.year,
        games=getattr(client, "week_games", {}) if week is not None else {},
        opponent_strength=getattr(client, "opponent_strength", {}),
        now=time.time(),
    )
    waiver_defenses = [row for row in defense_streams if not row.item.is_rostered
                       and row.item.market_status in {"waiver", "locked"}]
    balance = None
    activity = ()
    pricing_error = None
    if waiver_defenses:
        try:
            details = _cached_session_read(current, selected.id, "details", client.league_details, ttl=300)
            franchise = details.franchises.get(selected.franchise_id.zfill(4))
            balance = franchise.faab_balance if franchise else None
            activity = _cached_session_read(
                current, selected.id, "activity", lambda: client.transactions(days=21, count=200), ttl=90,
            )
        except MFLApiError as caught:
            _log_provider_error_once(current, selected.id, "defense_pricing_unavailable", caught)
            pricing_error = "Recent pricing or budget data is unavailable. Suggestions may use a budget-only heuristic."
    defense_pricing = defense_waiver_pricing(
        waiver_defenses, balance=balance, transactions=activity,
        catalog=current.player_catalog or {}, now=time.time(),
    )
    fantasy_teams = sorted(
        {
            (item.fantasy_team_id, item.fantasy_team_name)
            for item in recommendations
            if item.fantasy_team_id
        },
        key=lambda item: item[1].casefold(),
    )
    return templates.TemplateResponse(
        request=request,
        name="moves.html",
        context={
            "session": current,
            "league": selected,
            "roster": sorted(roster, key=lambda player: (player.position, player.name.casefold())),
            "recommendations": recommendations,
            "positions": positions,
            "board_positions": {item.player.id: _board_position(item.player) for item in recommendations},
            "defense_targets": [row for row in defense_streams if not row.item.is_rostered][:6],
            "owned_defenses": [row for row in defense_streams if row.item.market_status == "mine"],
            "talent_source_url": TALENT_SOURCE_URL,
            "talent_source_date": TALENT_SOURCE_DATE,
            "defense_pricing": defense_pricing,
            "defense_pricing_error": pricing_error,
            "nfl_teams": nfl_teams,
            "fantasy_teams": fantasy_teams,
            "week": week,
            "player_count": len(recommendations),
            "available_count": sum(not item.is_rostered for item in recommendations),
            "rostered_count": sum(item.is_rostered for item in recommendations),
            "locked_count": sum(item.availability.locked for item in recommendations),
            "projected_count": sum(item.projection is not None for item in recommendations),
            "projection_source": blend.source_label,
            "ml_matched": blend.ml_matched,
            "espn_matched": blend.espn_matched,
            "espn_source": blend.espn_source,
            "mfl_projections": blend.mfl_scores,
            "ml_projections": blend.ml_scores,
            "espn_ranks": blend.espn_ranks or {},
            "ytd_scores": getattr(client, "player_ytd_scores", {}),
            "avg_scores": getattr(client, "player_avg_scores", {}),
            "median_scores": getattr(client, "player_median_scores", {}),
            "median_window": getattr(client, "player_median_window", 0),
            "opponent_strength": getattr(client, "opponent_strength", {}),
            "roster_locked": roster_locked,
            "query": query,
            "error": api_error,
        },
    )


@app.get("/lineup", response_class=HTMLResponse)
def lineup_page(request: Request, league: str, error: str = "", week: int | None = None):
    current = _session(request)
    if not current:
        return RedirectResponse("/", status_code=303)
    selected = _league(current, league)
    client = _client(current, selected)
    api_error: str | None = error[:300] or None
    requested_week = week if week is not None else current.selected_week
    recommendation: LineupRecommendation | None = None
    settings = None
    blend = ProjectionBlend(scores={}, mfl_scores={}, ml_scores={}, ml_matched=0)
    try:
        week, recommendation, settings, blend = (
            _load_lineup(client, requested_week=requested_week, current=current)
            if requested_week is not None else _load_lineup(client, current=current)
        )
        _remember_catalog(current, client)
    except MFLApiError as caught:
        _log_provider_error_once(
            current,
            selected.id,
            "lineup_load_failed",
            caught,
            window=caught.retry_after if isinstance(caught, MFLRateLimitError) else 120,
        )
        api_error = str(caught)
    if week is not None and 1 <= week <= 18:
        current.selected_week = week
    return templates.TemplateResponse(
        request=request,
        name="lineup.html",
        context={
            "session": current,
            "league": selected,
            "week": week,
            "recommendation": recommendation,
            "slot_specs": lineup_slots(settings) if settings else [],
            "current_slots": {player.id: slot for slot,player in assign_lineup_slots((item.player for item in recommendation.players if item.currently_starting), settings) if player} if recommendation and settings else {},
            "lineup_visible": getattr(client, "lineup_visible", True),
            "games": getattr(client, "week_games", {}),
            "schedule_complete": getattr(client, "week_schedule_complete", False),
            "player_scores": getattr(client, "lineup_scores", {}),
            "ytd_scores": getattr(client, "player_ytd_scores", {}),
            "avg_scores": getattr(client, "player_avg_scores", {}),
            "median_scores": getattr(client, "player_median_scores", {}),
            "median_window": getattr(client, "player_median_window", 0),
            "opponent_strength": getattr(client, "opponent_strength", {}),
            "settings": settings,
            "projection_source": blend.source_label,
            "ml_matched": blend.ml_matched,
            "espn_matched": blend.espn_matched,
            "espn_source": blend.espn_source,
            "mfl_projections": blend.mfl_scores,
            "ml_projections": blend.ml_scores,
            "espn_ranks": blend.espn_ranks or {},
            "locked_count": sum(
                item.locked for item in recommendation.players
            ) if recommendation else 0,
            "error": api_error,
        },
    )


@app.get("/scores", response_class=HTMLResponse)
def scores_page(request: Request, league: str, week: int | None = None, matchup: str = "", view: str = "matchup"):
    current = _session(request)
    if not current:
        return RedirectResponse("/", status_code=303)
    selected = _league(current, league)
    if view not in {"matchup", "all"}:
        raise HTTPException(status_code=400, detail="Choose matchup or league scoreboard")
    try:
        matchup_index = int(matchup) if matchup else None
    except ValueError:
        raise HTTPException(status_code=400, detail="Choose a valid matchup")
    client = _client(current, selected)
    api_error: str | None = None
    selected_week: int | None = None
    current_week: int | None = None
    live = None
    franchise_names: dict[str, str] = {}
    head_to_head: HeadToHeadView | None = None
    try:
        selected_week, current_week, live, franchise_names, head_to_head = (
            _load_live_scoring_week(client, requested_week=week if week is not None else current.selected_week,
                                   current=current,
                                   **({"matchup_index": matchup_index} if matchup_index is not None else {}))
        )
        _remember_catalog(current, client)
    except (MFLApiError, ValueError) as caught:
        if isinstance(caught, MFLApiError):
            _log_provider_error_once(current, selected.id, "scores_load_failed", caught)
        else:
            log_error("scores_load_failed", caught)
        api_error = str(caught)
    if selected_week is not None:
        current.selected_week = selected_week
    refresh_state = {"active": False, "next_kickoff": None}
    if selected_week is not None and selected_week == current_week:
        try:
            refresh_state = _cached_session_read(
                current, selected.id, f"refresh-state:{selected_week}",
                lambda: client.nfl_refresh_state(week=selected_week, now=time.time()),
                ttl=30, stale_ttl=120,
            )
        except (MFLApiError, AttributeError) as exc:
            log_error("nfl_refresh_state_unavailable", exc)
    return templates.TemplateResponse(
        request=request,
        name="scores.html",
        context={
            "session": current,
            "league": selected,
            "week": selected_week,
            "current_week": current_week,
            "weeks": range(1, 19),
            "live": live,
            "franchise_names": franchise_names,
            "head_to_head": head_to_head,
            "refresh_seconds": 60 if refresh_state["active"] else 0,
            "next_kickoff": refresh_state["next_kickoff"],
            "viewed_matchup": matchup_index,
            "score_view": view,
            "error": api_error,
        },
    )


@app.get("/api/live-window")
def live_window(request: Request, league: str, week: int):
    current = _require_session(request)
    selected = _league(current, league)
    if not 1 <= week <= 18:
        raise HTTPException(status_code=400, detail="Choose a valid week")
    client = _client(current, selected)
    try:
        current_week = _cached_session_read(
            current, selected.id, "week", client.current_week, ttl=300, stale_ttl=86400,
        )
        if current_week != week:
            return {"active": False, "next_kickoff": None}
        return _cached_session_read(
            current, selected.id, f"refresh-state:{week}",
            lambda: client.nfl_refresh_state(week=week, now=time.time()),
            ttl=30, stale_ttl=120,
        )
    except MFLApiError:
        # Uncertain game state must not turn into round-the-clock score polling.
        return {"active": False, "next_kickoff": None}


@app.post("/lineup/preview")
def preview_lineup(
    request: Request,
    csrf_token: str = Form(...),
    league_id: str = Form(...),
    week: int = Form(...),
    starter_ids: list[str] = Form(default=[]),
):
    current = _require_session(request)
    _check_csrf(current, csrf_token)
    league = _league(current, league_id)
    client = _client(current, league)
    try:
        if not 1 <= week <= 18:
            raise ValueError("Choose a week from 1 through 18")
        roster_ids = client.roster_ids()
        selected_ids = set(starter_ids)
        if not selected_ids.issubset(roster_ids):
            raise ValueError("The lineup contains a player who is not on your MFL roster")
        settings = client.lineup_settings()
        selected_players = client.named_players(selected_ids)
        statuses = client.player_roster_statuses(roster_ids, week=week)
        roster = client.named_players(roster_ids)
        kickoffs = client.nfl_team_kickoffs(week=week)
        locked_ids = _locked_player_ids(roster, kickoffs)
        _check_locked_players_unchanged(
            selected_ids=selected_ids,
            statuses=statuses,
            locked_ids=locked_ids,
        )
        unavailable = [
            player.name
            for player in selected_players
            if statuses.get(player.id, "R") in {"IR", "TS"}
        ]
        if unavailable:
            raise ValueError(f"IR or taxi players cannot start: {', '.join(unavailable)}")
        if not lineup_is_legal(selected_players, settings):
            rules = ", ".join(
                f"{rule.name} {rule.minimum}-{rule.maximum}"
                if rule.minimum != rule.maximum
                else f"{rule.name} {rule.minimum}"
                for rule in settings.rules
            )
            raise ValueError(
                f"That is not a legal {settings.starter_count}-player lineup"
                + (f" ({rules})" if rules else "")
            )
        current_ids = {
            player_id for player_id, status in statuses.items() if status == "S"
        }
        current_players = client.named_players(current_ids)
        try:
            mfl_projections = client.projected_scores(
                week=week, player_ids=roster_ids
            )
        except MFLApiError:
            mfl_projections = {}
        projections = projection_blend(
            roster,
            year=current.year,
            week=week,
            mfl_scores=mfl_projections,
            session=client.session,
        ).scores
        preview = LineupPreview(
            league_id=league_id,
            franchise_id=league.franchise_id,
            week=week,
            current_starters=tuple(
                sorted(current_players, key=lambda player: (player.position, player.name))
            ),
            starters=tuple(
                sorted(selected_players, key=lambda player: (player.position, player.name))
            ),
            current_projection=sum(projections.get(player.id, 0.0) for player in current_players),
            projected_total=sum(projections.get(player.id, 0.0) for player in selected_players),
        )
        _remember_catalog(current, client)
    except (MFLApiError, ValueError) as caught:
        log_error("lineup_review_failed", caught)
        query = urlencode({"league": league_id, "week": week, "error": str(caught), "ref": request.state.error_reference})
        return RedirectResponse(f"/lineup?{query}", status_code=303)
    pending_id = secrets.token_urlsafe(24)
    current.pending_lineups[pending_id] = preview
    return RedirectResponse(f"/lineup/preview/{pending_id}", status_code=303)


@app.get("/lineup/preview/{pending_id}", response_class=HTMLResponse)
def show_lineup_preview(request: Request, pending_id: str):
    current = _session(request)
    if not current:
        return RedirectResponse("/", status_code=303)
    preview = current.pending_lineups.get(pending_id)
    if not preview:
        raise HTTPException(status_code=404, detail="This lineup preview expired")
    league = _league(current, preview.league_id)
    current_ids = {player.id for player in preview.current_starters}
    starter_ids = {player.id for player in preview.starters}
    return templates.TemplateResponse(
        request=request,
        name="lineup_preview.html",
        context={
            "session": current,
            "league": league,
            "preview": preview,
            "pending_id": pending_id,
            "starts": [player for player in preview.starters if player.id not in current_ids],
            "sits": [player for player in preview.current_starters if player.id not in starter_ids],
            "error": None,
            "success": None,
        },
    )


@app.get("/lineup/preview", include_in_schema=False)
def revisit_lineup_preview(request: Request):
    """Handle incomplete or stale lineup-preview links without a 405 error."""
    current = _session(request)
    return RedirectResponse("/dashboard" if current else "/", status_code=303)


@app.post("/lineup/submit/{pending_id}", response_class=HTMLResponse)
def submit_lineup(request: Request, pending_id: str, csrf_token: str = Form(...)):
    current = _require_session(request)
    _check_csrf(current, csrf_token)
    preview = current.pending_lineups.pop(pending_id, None)
    if not preview:
        raise HTTPException(status_code=404, detail="This lineup preview expired")
    league = _league(current, preview.league_id)
    client = _client(current, league)
    try:
        roster_ids = client.roster_ids()
        roster = client.named_players(roster_ids)
        statuses = client.player_roster_statuses(roster_ids, week=preview.week)
        selected_ids = {player.id for player in preview.starters}
        if not selected_ids.issubset(roster_ids):
            raise ValueError("Your roster changed. Reopen the lineup and review it again.")
        if any(statuses.get(player_id) in {"IR", "TS"} for player_id in selected_ids):
            raise ValueError("A selected player is now on IR or taxi. Review the lineup again.")
        if not lineup_is_legal(client.named_players(selected_ids), client.lineup_settings()):
            raise ValueError("This lineup no longer meets your league rules. Review it again.")
        kickoffs = client.nfl_team_kickoffs(week=preview.week)
        locked_ids = _locked_player_ids(roster, kickoffs)
        _check_locked_players_unchanged(
            selected_ids={player.id for player in preview.starters},
            statuses=statuses,
            locked_ids=locked_ids,
        )
        try:
            result = client.submit_lineup(
                week=preview.week,
                starter_ids=[player.id for player in preview.starters],
            )
            success = f"MFL accepted your Week {preview.week} lineup."
        except MFLWriteUncertainError as write_error:
            # Never retry an ambiguous write. Confirm the authoritative saved
            # starters with a separate read before reporting success.
            try:
                saved_statuses = client.player_roster_statuses(
                    roster_ids, week=preview.week
                )
            except MFLApiError as verify_error:
                log_error("lineup_submit_readback_failed", verify_error)
                raise MFLWriteUncertainError(
                    "MFL's response could not be confirmed. Do not submit again "
                    "until you check the Week "
                    f"{preview.week} lineup on MFL."
                ) from write_error
            saved_starters = {
                player_id
                for player_id, status in saved_statuses.items()
                if status == "S"
            }
            if saved_starters != selected_ids:
                raise write_error
            result = {"status": "verified-by-lineup-readback"}
            success = (
                f"MFL saved your Week {preview.week} lineup. "
                "The starters were verified after submission."
            )
        current.read_cache.pop(
            f"{current.year}:{league.id}:report:live-scoring:{preview.week}", None,
        )
        current.read_cache.pop(
            f"{current.year}:{league.id}:report:roster-status:"
            f"{league.franchise_id}:{preview.week}",
            None,
        )
        error = None
    except (MFLApiError, ValueError) as caught:
        log_error("lineup_submit_failed", caught)
        result = None
        success = None
        error = str(caught)
    return templates.TemplateResponse(
        request=request,
        name="lineup_preview.html",
        context={
            "session": current,
            "league": league,
            "preview": preview,
            "pending_id": None,
            "starts": [],
            "sits": [],
            "error": error,
            "success": success,
            "result": result,
        },
        status_code=200 if success else 502,
    )


@app.get("/lineup/submit/{pending_id}", include_in_schema=False)
def revisit_lineup_submission(request: Request, pending_id: str):
    """Turn refreshes or copied confirmation URLs back into safe GET pages."""
    current = _session(request)
    if not current:
        return RedirectResponse("/", status_code=303)
    if pending_id in current.pending_lineups:
        return RedirectResponse(f"/lineup/preview/{pending_id}", status_code=303)
    return RedirectResponse("/dashboard", status_code=303)
@app.post("/preview")
def preview_move(
    request: Request,
    csrf_token: str = Form(...),
    league_id: str = Form(...),
    add_id: str = Form(...),
    drop_id: str = Form(...),
    mode: Literal["fcfs", "waiver", "blind-bid"] = Form("fcfs"),
    bid: str = Form(""),
    round_number: str = Form(""),
    replace_existing: str = Form(""),
):
    current = _require_session(request)
    _check_csrf(current, csrf_token)
    try:
        pending_id, _, _ = _stage_move(
            current,
            league_id=league_id,
            add_id=add_id,
            drop_id=drop_id,
            mode=mode,
            bid=_optional_int(bid, "Bid"),
            round_number=_optional_int(round_number, "Round"),
            replace_existing=replace_existing == "1",
        )
    except (MFLApiError, ValueError) as error:
        log_error("move_review_failed", error)
        query = urlencode({"league": league_id, "error": str(error), "ref": request.state.error_reference})
        return RedirectResponse(
            f"/moves?{query}", status_code=303
        )
    return RedirectResponse(f"/preview/{pending_id}", status_code=303)


@app.get("/preview/{pending_id}", response_class=HTMLResponse)
def show_preview(request: Request, pending_id: str):
    current = _session(request)
    if not current:
        return RedirectResponse("/", status_code=303)
    preview = current.pending_moves.get(pending_id)
    if not preview:
        raise HTTPException(status_code=404, detail="This move preview expired")
    league = _league(current, preview.league_id)
    return templates.TemplateResponse(
        request=request,
        name="preview.html",
        context={
            "session": current,
            "league": league,
            "preview": preview,
            "pending_id": pending_id,
            "error": None,
            "success": None,
        },
    )


@app.post("/submit/{pending_id}", response_class=HTMLResponse)
def submit_move(request: Request, pending_id: str, csrf_token: str = Form(...)):
    current = _require_session(request)
    _check_csrf(current, csrf_token)
    preview = current.pending_moves.pop(pending_id, None)
    if not preview:
        raise HTTPException(status_code=404, detail="This move preview expired")
    league = _league(current, preview.league_id)
    client = _client(current, league)
    try:
        client.validate_add_drop(preview)
        week = client.current_week()
        if week is not None:
            locked = _locked_player_ids(
                [preview.add, preview.drop],
                client.nfl_team_kickoffs(week=week),
            )
            blocked = {
                player_id for player_id in locked
                if player_id == preview.drop.id or preview.mode == "fcfs"
            }
            if blocked:
                names = ", ".join(
                    player.name
                    for player in (preview.add, preview.drop)
                    if player.id in blocked
                )
                raise ValueError(f"Game already started; MFL has locked: {names}")
        try:
            result = client.submit_add_drop(preview, replace=preview.replace_existing)
        except MFLWriteUncertainError as write_error:
            # MFL occasionally follows a successful FCFS write with an HTML
            # page instead of API JSON/XML. Verify authoritative roster state
            # before showing success; queued claims remain explicitly uncertain.
            if preview.mode == "fcfs":
                try:
                    current_roster = client.roster_ids()
                except MFLApiError as verify_error:
                    log_error("move_submit_readback_failed", verify_error)
                    raise MFLWriteUncertainError(
                        "MFL's response could not be confirmed. Do not submit again "
                        "until you check your MFL roster and Transactions report."
                    ) from write_error
                if preview.add.id in current_roster and preview.drop.id not in current_roster:
                    result = {"status": "verified-by-roster-readback"}
                else:
                    raise write_error
            else:
                raise write_error
        _invalidate_player_board(current, league.id)
        success = "MFL accepted the transaction request."
        error = None
    except (MFLApiError, ValueError) as api_error:
        log_error("move_submit_failed", api_error)
        result = None
        success = None
        error = str(api_error)
    return templates.TemplateResponse(
        request=request,
        name="preview.html",
        context={
            "session": current,
            "league": league,
            "preview": preview,
            "pending_id": None,
            "error": error,
            "success": success,
            "result": result,
        },
        status_code=200 if success else 502,
    )


@app.get("/trades", response_class=HTMLResponse)
def trades_page(request: Request, league: str, target: str = "", give: str = "", receive: str = "", wanted: str = "", package_size: int = 2):
    current = _session(request)
    if not current:
        return RedirectResponse("/", status_code=303)
    selected = _league(current, league)
    client = _client(current, selected)
    names, own_players, other_players, error = {}, [], [], None
    target = target.zfill(4) if target else ""
    try:
        league_names = _cached_session_read(current, selected.id, "franchise-names", client.franchise_names, ttl=300)
        names = {key.zfill(4): value for key, value in league_names.items()
                 if key.zfill(4) not in {selected.franchise_id.zfill(4), "0000"}}
        if target and target not in names:
            raise ValueError("Choose another team in this league.")
        # Only read the two teams the owner is working with. Loading every
        # league roster made a failed large export block every trade partner.
        own = selected.franchise_id.zfill(4)
        rosters = {
            own: _cached_session_read(
                current, selected.id, f"roster:{own}", lambda: client.franchise_roster(own),
                ttl=30, stale_ttl=300,
            )
        }
        if target:
            rosters[target] = _cached_session_read(
                current, selected.id, f"roster:{target}", lambda: client.franchise_roster(target),
                ttl=30, stale_ttl=300,
            )
        catalog = _cached_session_read(
            current, selected.id, "players", client.players, ttl=3600, stale_ttl=86400,
        )
        own = selected.franchise_id.zfill(4)
        if own not in rosters or (target and target not in rosters):
            raise ValueError("MFL has not made both rosters available.")
        def roster(team):
            return sorted((catalog.get(pid, MFLPlayer(pid, f"Player {pid}")) for pid in rosters.get(team, set())), key=lambda p: (p.position, p.name.casefold()))
        own_players, other_players = roster(own), roster(target)
        _remember_catalog(current, client)
    except (MFLApiError, ValueError) as exc:
        log_error("trade_rosters_failed", exc)
        error = str(exc) if isinstance(exc, ValueError) else "MFL could not load the selected roster. Use Load rosters to retry. If it keeps failing, disconnect and sign in again. No offer was sent."
    return templates.TemplateResponse(request=request, name="trades.html", context={
        "session": current, "league": selected, "teams": names, "target": target,
        "own_players": own_players, "other_players": other_players, "error": error, "week": current.selected_week,
        "suggested_give": set(give.split(",")), "suggested_receive": receive,
        "wanted": wanted, "package_size": package_size if package_size in {1,2} else 2,
    })


@app.post("/trades/preview")
def preview_trade(request: Request, league: str = Form(...), target: str = Form(...),
                  give: list[str] = Form(default=[]), receive: list[str] = Form(default=[]),
                  comments: str = Form(default="", max_length=500), csrf_token: str = Form(...)):
    current = _require_session(request)
    _check_csrf(current, csrf_token)
    selected = _league(current, league)
    client = _client(current, selected)
    try:
        target, give, receive = client.validate_player_trade(target, give, receive)
        catalog = client.players()
        names = {key.zfill(4): value for key, value in client.franchise_names().items()}
        if target not in names:
            raise ValueError("Choose another team in this league.")
        if any(pid not in catalog for pid in give + receive):
            raise ValueError("Player details are unavailable. Reload the rosters before reviewing.")
    except ValueError as exc:
        return templates.TemplateResponse(request=request, name="trade_review.html", status_code=400,
            context={"session": current, "league": selected, "draft": None, "validation_error": str(exc)})
    except MFLApiError as exc:
        log_error("trade_preview_failed", exc)
        return templates.TemplateResponse(request=request, name="trade_review.html", status_code=502,
            context={"session": current, "league": selected, "draft": None,
                     "validation_error": "MFL could not verify this trade. No offer was sent."})
    pending_id = secrets.token_urlsafe(24)
    with trade_send_lock:
        # Bounded session memory, including receipts used to prevent resubmission.
        if len(current.trades) >= 50:
            oldest = next((key for key, draft in current.trades.items() if draft.status != "sending"), None)
            if oldest is not None:
                current.trades.pop(oldest)
            else:
                raise HTTPException(status_code=429, detail="Wait for your current offers to finish.")
        current.trades[pending_id] = TradeDraft(selected.id, selected.franchise_id, target, names[target],
            tuple(catalog[p] for p in give), tuple(catalog[p] for p in receive), comments.strip())
    return RedirectResponse(f"/trades/review/{pending_id}", status_code=303)


@app.get("/trades/review/{pending_id}", response_class=HTMLResponse)
def review_trade(request: Request, pending_id: str):
    current = _require_session(request)
    draft = current.trades.get(pending_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="This trade preview has expired.")
    selected = _league(current, draft.league_id)
    return templates.TemplateResponse(request=request, name="trade_review.html", context={
        "session": current, "league": selected, "draft": draft, "pending_id": pending_id,
    })


@app.post("/trades/send/{pending_id}")
def send_trade(request: Request, pending_id: str, csrf_token: str = Form(...)):
    current = _require_session(request)
    _check_csrf(current, csrf_token)
    with trade_send_lock:
        draft = current.trades.get(pending_id)
        if draft is None:
            raise HTTPException(status_code=404, detail="This trade preview has expired.")
        selected = _league(current, draft.league_id)
        if draft.status != "draft":
            return RedirectResponse(f"/trades/review/{pending_id}", status_code=303)
        if selected.franchise_id != draft.franchise_id or time.monotonic() - draft.created_at > 1200:
            draft.status, draft.message = "failed", "This preview expired. Rebuild the offer from current rosters."
            return RedirectResponse(f"/trades/review/{pending_id}", status_code=303)
        draft.status = "sending"
    try:
        result = _client(current, selected).propose_player_trade(target=draft.target,
            give=[p.id for p in draft.give], receive=[p.id for p in draft.receive], comments=draft.comments)
        status = result.get("status", "") if isinstance(result, dict) else ""
        status = status.get("$t", "") if isinstance(status, dict) else status
        if str(status).strip().casefold() not in {"ok", "success", "1"}:
            raise MFLApiError("Unrecognized trade acknowledgement")
        draft.status, draft.message = "sent", "MFL confirmed your trade offer was sent. It is not a completed trade; the other owner must respond and league rules still apply."
    except ValueError as exc:
        draft.status, draft.message = "failed", str(exc)
        log_error("trade_validation_failed", exc)
    except Exception as exc:
        # A timeout or unexpected acknowledgement may occur after MFL accepted
        # the offer. Keep this receipt consumed and never silently retry a write.
        draft.status = "uncertain"
        draft.message = "MFL did not confirm the offer. Check Pending Trades on MFL before creating another offer. This request will not be sent again."
        log_error("trade_send_unconfirmed", exc)
    return RedirectResponse(f"/trades/review/{pending_id}", status_code=303)


@app.post("/trade-block/preview")
def preview_block(request: Request, league: str = Form(...), player_ids: list[str] = Form(default=[]),
                  wanted: str = Form(default="", max_length=256), csrf_token: str = Form(...)):
    current = _require_session(request)
    _check_csrf(current, csrf_token)
    selected = _league(current, league)
    client = _client(current, selected)
    try:
        ids = set(player_ids)
        if not ids or len(ids) > 100 or not all(p.isdecimal() for p in ids):
            raise ValueError("Choose at least one player from your current roster.")
        if not ids <= client.trade_rosters().get(selected.franchise_id.zfill(4), set()):
            raise ValueError("A selected player is no longer on your roster.")
        catalog = client.players()
        if not ids <= catalog.keys():
            raise ValueError("Player details are unavailable. Reload the trade block.")
        expected = client.trade_block().get(selected.franchise_id.zfill(4), {"assets": (), "wanted": "", "timestamp": ""})
        assets = tuple(sorted(set(expected["assets"]) | ids))
        draft = BlockDraft(selected.id, selected.franchise_id, tuple(catalog[p] for p in sorted(ids)),
                           expected, wanted.strip(), tuple(catalog[a].name if a in catalog else a for a in assets))
    except (MFLApiError, ValueError) as exc:
        log_error("block_preview_failed", exc)
        return templates.TemplateResponse(request=request, name="block_review.html", status_code=400,
            context={"session": current, "league": selected, "draft": None,
                     "error": str(exc) if isinstance(exc, ValueError) else "MFL could not verify the existing block. Nothing was changed."})
    pending_id = secrets.token_urlsafe(24)
    with trade_send_lock:
        if len(current.blocks) >= 50:
            oldest = next((key for key, item in current.blocks.items() if item.status != "sending"), None)
            if oldest is None:
                raise HTTPException(status_code=429, detail="Wait for your current update to finish.")
            current.blocks.pop(oldest)
        current.blocks[pending_id] = draft
    return RedirectResponse(f"/trade-block/review/{pending_id}", status_code=303)


@app.get("/trade-block/review/{pending_id}", response_class=HTMLResponse)
def review_block(request: Request, pending_id: str):
    current = _require_session(request)
    draft = current.blocks.get(pending_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="This trade-block preview expired.")
    return templates.TemplateResponse(request=request, name="block_review.html", context={
        "session": current, "league": _league(current, draft.league_id), "draft": draft, "pending_id": pending_id})


@app.post("/trade-block/send/{pending_id}")
def send_block(request: Request, pending_id: str, csrf_token: str = Form(...)):
    current = _require_session(request)
    _check_csrf(current, csrf_token)
    with trade_send_lock:
        draft = current.blocks.get(pending_id)
        if draft is None:
            raise HTTPException(status_code=404, detail="This trade-block preview expired.")
        selected = _league(current, draft.league_id)
        if draft.status != "draft":
            return RedirectResponse(f"/trade-block/review/{pending_id}", status_code=303)
        if (selected.franchise_id != draft.franchise_id or time.monotonic() - draft.created_at > 1200
                or any(item is not draft and item.league_id == draft.league_id and item.status == "sending"
                       for item in current.blocks.values())):
            draft.status, draft.message = "failed", "This preview expired or another update is running. Reload the block before trying again."
            return RedirectResponse(f"/trade-block/review/{pending_id}", status_code=303)
        draft.status = "sending"
    try:
        client = _client(current, selected)
        result = client.add_to_trade_block([p.id for p in draft.players], draft.expected, draft.wanted)
        status = result.get("status", "") if isinstance(result, dict) else ""
        status = status.get("$t", "") if isinstance(status, dict) else status
        if str(status).strip().casefold() not in {"ok", "success", "1"}:
            raise MFLApiError("Unrecognized trade-block acknowledgement")
        saved = client.trade_block().get(selected.franchise_id.zfill(4))
        wanted_assets = set(draft.expected["assets"]) | {p.id for p in draft.players}
        if not saved or set(saved["assets"]) != wanted_assets or saved["wanted"] != draft.wanted:
            raise MFLApiError("Trade-block readback did not match")
        draft.status, draft.message = "sent", "Your trade block is updated on MFL. Existing assets were preserved. No trade offer was sent."
    except ValueError as exc:
        draft.status, draft.message = "failed", str(exc)
    except Exception as exc:
        log_error("block_update_unconfirmed", exc)
        draft.status, draft.message = "uncertain", "MFL did not confirm the saved block. Check your trade block before trying again. This request will not be repeated."
    return RedirectResponse(f"/trade-block/review/{pending_id}", status_code=303)


@app.post("/logout")
def logout(request: Request, csrf_token: str = Form(...)):
    current = _require_session(request)
    _check_csrf(current, csrf_token)
    session_id = request.cookies.get("wp_session")
    with sessions_lock:
        sessions.pop(session_id or "", None)
    remember_token = current.remember_token or request.cookies.get("wp_remember", "")
    if remember_token:
        try:
            _persistent_store().revoke(remember_token)
        except Exception as error:
            log_error("remembered_session_revoke_failed", error)
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie("wp_session", path="/", httponly=True, samesite="strict", secure=_secure_cookies(request))
    response.delete_cookie("wp_remember", path="/", httponly=True, samesite="strict", secure=_secure_cookies(request))
    response.headers["Clear-Site-Data"] = '"cache"'
    return response


@app.get("/api/scoring/{player_id}")
def api_starter_scoring(request: Request, player_id: str, league: str, franchise: str, week: int):
    current = _require_session(request)
    selected = _league(current, league)
    if not 1 <= week <= 18:
        raise HTTPException(status_code=400, detail="Choose a valid week")
    client = _client(current, selected)
    # Recheck the saved lineup: a stale page or manually constructed URL cannot
    # request scoring details for a benched player. Franchise matters in leagues
    # allowing duplicate players on different rosters.
    try:
        live = _cached_session_read(
            current, selected.id, f"live-scoring:{week}",
            lambda: client.live_scoring(week=week), ttl=30, stale_ttl=300,
        )
    except MFLApiError as error:
        _log_provider_error_once(current, selected.id, "scoring_detail_snapshot_unavailable", error)
        raise HTTPException(
            status_code=503,
            detail="MFL scoring is temporarily busy. The official score on the matchup remains available.",
        ) from error
    item = next((p for match in live.matchups for team in match.franchises
                 if team.franchise_id == franchise for p in team.players
                 if p.player_id == player_id and p.status.casefold() in {"starter", "s"}), None)
    if item is None:
        raise HTTPException(status_code=409, detail="This player is not a saved starter for this team and week. Reload the matchup.")
    try:
        player = _cached_session_read(
            current, selected.id, "players", client.players, ttl=3600, stale_ttl=86400,
        ).get(player_id)
    except MFLApiError as error:
        _log_provider_error_once(current, selected.id, "scoring_detail_catalog_unavailable", error)
        raise HTTPException(
            status_code=503, detail="Player details are temporarily unavailable. The official score remains on the matchup."
        ) from error
    if player is None:
        raise HTTPException(status_code=404, detail="Player not found")
    result = {"official_points": item.score, "stat_lines": [], "components": [],
              "state": "Upcoming", "difference": None, "source": "Official points: MFL · Box score: ESPN",
              "note": "Stats will appear after kickoff."}
    if item.game_seconds_remaining >= 3600:
        return result
    result["state"] = "Final" if item.game_seconds_remaining <= 0 else "Live"
    result["note"] = "Detailed stats are unavailable. Your official MFL points are still shown."
    try:
        box = weekly_boxscore(player, current.year, week)
        if box:
            result.update(state=box["state"], stat_lines=box["stat_lines"])
            result["note"] = "Stat points use this league’s supported scoring rules. MFL’s total is authoritative; feeds may update at different times."
            try:
                rules = _cached_session_read(
                    current, selected.id, "scoring-rules", client.scoring_rules,
                    ttl=_SCORING_RULES_TTL, stale_ttl=_SCORING_RULES_STALE_TTL,
                )
                result["components"] = scoring_components(box, rules, player.position)
            except MFLApiError as error:
                _log_provider_error_once(current, selected.id, "scoring_detail_rules_unavailable", error)
                result["note"] = "League rules are unavailable. Stats and official MFL points are shown separately."
            result["difference"] = round(item.score - sum(c["points"] for c in result["components"]), 2)
    except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError) as error:
        log_error("scoring_detail_stats_unavailable", error)
    return result


@app.get("/api/players/{player_id}")
def api_player_card(request: Request, player_id: str, league: str, week: int | None = None):
    current = _require_session(request)
    selected = _league(current, league)
    client = _client(current, selected)
    try:
        player = _cached_session_read(
            current, selected.id, "players", client.players, ttl=3600, stale_ttl=86400,
        ).get(player_id)
    except MFLApiError as error:
        _log_provider_error_once(current, selected.id, "player_card_catalog_unavailable", error)
        raise HTTPException(status_code=503, detail="Player details are temporarily unavailable.") from error
    if not player:
        raise HTTPException(status_code=404, detail="Player not found")
    _remember_catalog(current, client)
    selected_week = week if week is not None else current.selected_week
    if selected_week is None:
        try:
            selected_week = _cached_session_read(
                current, selected.id, "week", client.current_week, ttl=300, stale_ttl=86400,
            )
        except MFLApiError as error:
            _log_provider_error_once(current, selected.id, "player_card_week_unavailable", error)
            raise HTTPException(status_code=503, detail="The current MFL week is temporarily unavailable.") from error
    if selected_week is None or not 1 <= selected_week <= 18:
        raise HTTPException(status_code=400, detail="Choose a valid week")
    projection = actual = None
    scoring = []
    try:
        projections = _cached_session_read(
            current, selected.id, f"projections:{selected_week}",
            lambda: client.projected_scores(week=selected_week), ttl=300, stale_ttl=86400,
        )
        projection = projections.get(player_id)
        live = _cached_session_read(
            current, selected.id, f"live-scoring:{selected_week}",
            lambda: client.live_scoring(week=selected_week), ttl=30, stale_ttl=300,
        )
        actual = next((item.score for matchup in live.matchups for franchise in matchup.franchises for item in franchise.players if item.player_id == player_id), None)
    except MFLApiError as error:
        _log_provider_error_once(current, selected.id, "player_card_points_unavailable", error)
    try:
        rules = _cached_session_read(
            current, selected.id, "scoring-rules", client.scoring_rules,
            ttl=_SCORING_RULES_TTL, stale_ttl=_SCORING_RULES_STALE_TTL,
        ).get("positionRules", [])
        if isinstance(rules, dict):
            rules = [rules]
        position = {"K":"PK", "DST":"DEF"}.get(player.position.upper(), player.position.upper())
        for group in rules:
            if position not in str(group.get("positions", "")).upper().split("|"):
                continue
            group_rules = group.get("rule", [])
            if isinstance(group_rules, dict):
                group_rules = [group_rules]
            for rule in group_rules:
                def value(key):
                    result = rule.get(key, "")
                    return str(result.get("$t", "")) if isinstance(result, dict) else str(result)
                scoring.append({"event":value("event"), "range":value("range"), "points":value("points")})
    except MFLApiError as error:
        _log_provider_error_once(current, selected.id, "player_card_rules_unavailable", error)
    return {
        "name": player.name, "position": player.position, "team": player.team,
        "photo": player.photo_url, "jersey": player.jersey, "college": player.college,
        "height": player.height, "weight": player.weight, "draft_year": player.draft_year,
        "source": "Player details: MyFantasyLeague · Photos: ESPN",
        "week": selected_week, "projection": projection, "actual_points": actual,
        "scoring_league": selected.name, "scoring_rules": scoring,
        "projection_source": "FantasySharks raw projections scored by MFL with this league's rules",
    }


class BrowserErrorRequest(BaseModel):
    kind: Literal["script", "promise"]
    page: Literal["/dashboard", "/lineup", "/moves", "/scores"]
    line: int = Field(default=0, ge=0, le=1_000_000)


@app.post("/api/client-error")
def api_client_error(request: Request, report: BrowserErrorRequest):
    current = _require_session(request)
    _check_csrf(current, request.headers.get("X-CSRF-Token", ""))
    # Only validated categories and a bounded line number. Never accept JS error text.
    log_error(f"browser_{report.page[1:]}_{report.kind}_line_{report.line}")
    return {"reference": request.state.error_reference}


class StageMoveRequest(BaseModel):
    league_id: str
    add_id: str
    drop_id: str
    mode: Literal["fcfs", "waiver", "blind-bid"] = "fcfs"
    bid: int | None = None
    round: int | None = None
    replace_existing: bool = False


@app.get("/api/free-agents")
def api_free_agents(request: Request, league: str, q: str = ""):
    current = _require_session(request)
    selected = _league(current, league)
    client = _client(current, selected)
    week, _, board, blend, _ = _load_player_board(client, current)
    needle = q.strip()[:80].casefold()
    matches = [
        item for item in board
        if not item.is_rostered and needle in item.player.name.casefold()
    ]
    _remember_catalog(current, client)
    return {
        "league_id": league,
        "week": week,
        "projection_source": blend.source_label,
        "players": [
            {
                "id": item.player.id,
                "name": item.player.name,
                "position": item.player.position,
                "team": item.player.team,
                "status": item.availability.label,
                "locked": item.availability.locked,
                "projection": item.projection,
                "roster_delta": item.roster_delta,
                "recommendation": item.recommendation,
            }
            for item in matches
        ],
    }


@app.post("/api/stage-move")
def api_stage_move(request: Request, move: StageMoveRequest):
    current = _require_session(request)
    csrf_token = request.headers.get("X-CSRF-Token", "")
    _check_csrf(current, csrf_token)
    pending_id, preview, _ = _stage_move(
        current,
        league_id=move.league_id,
        add_id=move.add_id,
        drop_id=move.drop_id,
        mode=move.mode,
        bid=move.bid,
        round_number=move.round,
        replace_existing=move.replace_existing,
    )
    return JSONResponse(
        {
            "preview_url": f"/preview/{pending_id}",
            "add": preview.add.name,
            "drop": preview.drop.name,
            "submitted": False,
        }
    )


def run() -> None:
    uvicorn.run(app, host="127.0.0.1", port=8765, access_log=False)
