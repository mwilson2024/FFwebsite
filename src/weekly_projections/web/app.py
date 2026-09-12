from __future__ import annotations

import secrets
import os
import math
import time
from datetime import datetime
from dataclasses import dataclass, field
from itertools import zip_longest
from pathlib import Path
from threading import Lock
from typing import Literal
from urllib.parse import urlencode

import uvicorn
import requests
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from weekly_projections.mfl.client import (
    AddDropPreview,
    MFLAvailability,
    MFLApiError,
    MFLClient,
    MFLConfig,
    MFLLiveScoring,
    MFLLeague,
    MFLLineupSettings,
    MFLPlayer,
)
from weekly_projections.lineup import (
    LineupRecommendation,
    assign_lineup_slots,
    lineup_slots,
    lineup_is_legal,
    recommend_lineup,
)
from weekly_projections.projection_sources import ProjectionBlend, projection_blend
from weekly_projections.live_stats import weekly_boxscore, scoring_components
from weekly_projections.recommendations import PlayerRecommendation, build_player_board
from weekly_projections.trade_engine import suggest_trades, analyze_target_trade
from weekly_projections.league_intelligence import (
    build_power_rankings,
    build_recap,
    playoff_probability,
    waiver_trends,
)
from weekly_projections.web.diagnostics import initialize_log, log_error, request_context


PACKAGE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")

_TEAM_DEFENSE_POSITIONS = {"DEF", "DST", "D/ST"}
_INDIVIDUAL_DEFENSE_POSITIONS = {
    "CB", "DB", "DE", "DL", "DT", "EDGE", "ILB", "LB", "NT", "OLB", "S", "SAF",
}


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
    mfl_api_key: str = ""
    read_cache: dict[str, tuple[float, object]] = field(default_factory=dict)
    side_bets: dict[str, list[SideBet]] = field(default_factory=dict)


sessions: dict[str, BrowserSession] = {}
trade_send_lock = Lock()
initialize_log()
app = FastAPI(title="Weekly Projections · MFL Moves", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")


@app.middleware("http")
async def secure_local_responses(request: Request, call_next):
    request_id = secrets.token_hex(6)
    request.state.error_reference = request_id
    token = request_context.set((request_id, request))
    try:
        try:
            response = await call_next(request)
        except Exception as error:
            log_error("unhandled_request_error", error, status=500)
            response = HTMLResponse(
                f'<h1>Something went wrong</h1><p>Error reference: {request_id}</p>'
                '<p>The error was recorded locally. <a href="/dashboard">Return to your leagues</a></p>',
                status_code=500,
            )
        if response.status_code >= 400:
            log_error("http_error", status=response.status_code)
    finally:
        request_context.reset(token)
    response.headers["X-Error-Reference"] = request_id
    # Device-local navigation preference only. It never grants league access,
    # stores credentials, or follows background widget requests.
    current = _session(request)
    league_id = request.query_params.get("league")
    if (current and request.method == "GET" and response.status_code == 200
            and request.url.path in {"/home", "/lineup", "/moves", "/scores", "/trades", "/standings", "/league"}
            and any(item.id == league_id for item in current.leagues)):
        response.set_cookie("wp_last_league", f"{current.year}:{league_id}", max_age=365*86400,
                            httponly=True, samesite="strict", secure=_secure_cookies(request))
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self'; img-src 'self' data: https://a.espncdn.com https://*.myfantasyleague.com; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    )
    return response


def _secure_cookies(request: Request) -> bool:
    # Explicit hosting setting also works behind Azure's HTTPS terminator.
    return os.environ.get("WP_SECURE_COOKIES", "").lower() in {"1", "true"} or request.url.scheme == "https"


def _session(request: Request) -> BrowserSession | None:
    session_id = request.cookies.get("wp_session")
    current = sessions.get(session_id or "")
    if current and current.expires_at <= time.monotonic():
        sessions.pop(session_id, None)
        return None
    return current


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
            api_key=current.mfl_api_key or None,
        )
    )
    client._players = current.player_catalog
    # Short-lived, session-local read cache. Mutation previews and submissions
    # intentionally bypass it and re-read MFL before any write.
    client._browser_read_cache = current.read_cache
    return client


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
    details = client.league_details()
    names = {team_id: team.name for team_id, team in details.franchises.items()}

    def read(label, loader, fallback):
        try:
            return loader()
        except (MFLApiError, ValueError, requests.RequestException) as error:
            log_error(f"league_hq_{label}_unavailable", error)
            errors[label] = "MFL could not load this report right now."
            return fallback

    current_week = read("week", client.current_week, None) or details.start_week
    standings = read("standings", client.league_standings, [])
    schedule = read("schedule", client.fantasy_schedule, ())
    activity = read("activity", lambda: client.transactions(days=21, count=200), ())
    catalog = read("players", client.players, {})
    _remember_catalog(current, client)
    rankings = build_power_rankings(schedule, names, current_week=current_week)
    rank_by_team = {row.franchise_id: row for row in rankings}
    recap = build_recap(schedule, names, current_week=current_week)
    trends = waiver_trends(activity)
    playoff_games = []
    for game in schedule:
        if game.week <= details.last_regular_season_week:
            continue
        probability = None
        if len(game.team_ids) == 2:
            probability = playoff_probability(rank_by_team.get(game.team_ids[0]), rank_by_team.get(game.team_ids[1]))
        playoff_games.append({"game": game, "probability": probability})
    result = {
        "details": details,
        "teams": details.franchises,
        "names": names,
        "current_week": current_week,
        "standings": standings,
        "groups": _standings_groups(standings, details),
        "schedule": schedule,
        "activity": activity,
        "catalog": catalog,
        "rankings": rankings,
        "rank_by_team": rank_by_team,
        "recap": recap,
        "trends": trends[:12],
        "playoff_games": playoff_games,
        "errors": errors,
    }
    current.read_cache[cache_key] = (time.monotonic() + 90, result)
    return result


def _load_player_board(
    client: MFLClient,
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
            return cached[1]
    roster_ids = client.roster_ids()
    availability = client.free_agents()
    league_rosters = client.trade_rosters()
    league_rosters.setdefault(client.config.franchise_id.zfill(4), set()).update(roster_ids)
    rostered_by = {
        player_id: franchise_id.zfill(4)
        for franchise_id, player_ids in league_rosters.items()
        for player_id in player_ids
    }
    details = client.league_details()
    franchise_names = {
        franchise_id.zfill(4): franchise.name
        for franchise_id, franchise in details.franchises.items()
    }
    catalog = client.players()
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
    week = client.current_week()
    roster_locked: set[str] = set()
    bye_teams: set[str] = set()
    if week is not None:
        try:
            kickoffs = client.nfl_team_kickoffs(week=week)
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
        projections = client.projected_scores(week=week)
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
    result = (week, roster, recommendations, blend, roster_locked)
    if cache is not None:
        # This board combines several large MFL exports. A brief cache keeps
        # home widgets and the player page from immediately repeating them,
        # while submission-time ownership and lock checks remain live.
        cache[cache_key] = (time.monotonic() + 30, result)
    return result


def _load_lineup(
    client: MFLClient,
    requested_week: int | None = None,
) -> tuple[int, LineupRecommendation, MFLLineupSettings, ProjectionBlend]:
    week = requested_week if requested_week is not None else client.current_week()
    if week is None:
        raise MFLApiError("MFL did not return the current lineup week")
    if not 1 <= week <= 18:
        raise MFLApiError("Choose a week from 1 through 18")
    roster_ids = client.roster_ids()
    roster = client.named_players(roster_ids)
    settings = client.lineup_settings()
    statuses = client.player_roster_statuses(roster_ids, week=week)
    kickoffs = client.nfl_team_kickoffs(week=week)
    locked_player_ids = _locked_player_ids(roster, kickoffs)
    if any(statuses.get(player_id, "R") not in {"S","NS","IR","TS"} for player_id in locked_player_ids):
        raise MFLApiError("MFL has not exposed the saved lineup for a locked player. Reconnect MFL and reload this week; no lineup changes were made.")
    client.lineup_visible = any(value in {"S", "NS"} for value in statuses.values())
    client.lineup_scores = {}
    try:
        live = client.live_scoring(week=week)
        client.lineup_scores = {
            item.player_id: item.score for matchup in live.matchups
            for franchise in matchup.franchises
            if franchise.franchise_id == client.config.franchise_id
            for item in franchise.players
        }
    except MFLApiError as error:
        log_error("lineup_scores_unavailable", error)
    try:
        projections = client.projected_scores(week=week, player_ids=roster_ids)
    except MFLApiError:
        projections = {}
    try:
        injuries = client.injuries(week=week)
    except MFLApiError:
        injuries = {}
    blend = projection_blend(
        roster,
        year=client.config.year,
        week=week,
        mfl_scores=projections,
        session=client.session,
    )
    recommendation = recommend_lineup(
        roster=roster,
        settings=settings,
        projections=blend.scores,
        roster_statuses=statuses,
        injuries=injuries,
        locked_player_ids=locked_player_ids,
    )
    return week, recommendation, settings, blend


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
) -> tuple[int, int, MFLLiveScoring, dict[str, str], HeadToHeadView | None]:
    return _load_live_scoring_week(client, requested_week=None)


def _load_live_scoring_week(
    client: MFLClient,
    *,
    requested_week: int | None,
    matchup_index: int | None = None,
) -> tuple[int, int, MFLLiveScoring, dict[str, str], HeadToHeadView | None]:
    current_week = client.current_week()
    if current_week is None:
        raise MFLApiError("MFL did not return the current scoring week")
    week = requested_week if requested_week is not None else current_week
    if week < 1 or week > 18:
        raise ValueError("Choose an NFL week from 1 through 18")
    live = client.live_scoring(week=week)
    names = client.franchise_names()
    catalog = client.players()
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
        mfl_projections = client.projected_scores(week=week, player_ids=player_ids)
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
        if locked:
            names = ", ".join(
                player.name for player in (preview.add, preview.drop) if player.id in locked
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


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    current = _session(request)
    if current:
        return RedirectResponse("/dashboard", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": None, "year": 2026},
    )


@app.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    api_key: str = Form(""),
    year: int = Form(2026),
):
    api_key = api_key.strip()
    try:
        if api_key and (len(api_key) > 512 or any(character.isspace() or not character.isprintable() for character in api_key)):
            raise ValueError("Enter a valid MFL API key without spaces.")
        if not api_key and (not username.strip() or not password):
            raise ValueError("Enter your MFL username and password, or use an API key.")
        client = MFLClient(
            MFLConfig(
                year=year,
                league_id="",
                franchise_id="",
                username=username,
                password=password,
                api_key=api_key or None,
            )
        )
        client.login()
        leagues = [league for league in client.account_leagues() if league.franchise_id != "0000"]
        if not leagues:
            raise MFLApiError(f"No owner leagues were found for the {year} season")
        session_id = secrets.token_urlsafe(32)
        sessions[session_id] = BrowserSession(
            mfl_cookie="" if api_key else client.user_cookie(),
            year=year,
            leagues=leagues,
            csrf_token=secrets.token_urlsafe(32),
            mfl_api_key=api_key,
        )
    except (MFLApiError, ValueError) as error:
        log_error("login_failed", error)
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": str(error), "year": year},
            status_code=401,
        )

    response = RedirectResponse(_league_home_url(request, sessions[session_id]), status_code=303)
    response.set_cookie(
        "wp_session",
        session_id,
        httponly=True,
        samesite="strict",
        secure=_secure_cookies(request),
        max_age=8 * 60 * 60,
    )
    return response


@app.post("/session/api-key")
def add_session_api_key(
    request: Request,
    league: str = Form(...),
    api_key: str = Form(...),
    csrf_token: str = Form(...),
):
    current = _require_session(request)
    _check_csrf(current, csrf_token)
    selected = _league(current, league)
    api_key = api_key.strip()
    if not api_key or len(api_key) > 512 or any(character.isspace() or not character.isprintable() for character in api_key):
        return RedirectResponse(f"/home?league={selected.id}&connection=invalid-key", status_code=303)
    try:
        probe = MFLClient(MFLConfig(
            year=current.year,
            league_id=selected.id,
            franchise_id=selected.franchise_id,
            api_key=api_key,
        ))
        probe.league_standings()
    except (MFLApiError, requests.RequestException):
        log_error("api_key_validation_failed")
        return RedirectResponse(f"/home?league={selected.id}&connection=invalid-key", status_code=303)
    current.mfl_api_key = api_key
    return RedirectResponse(f"/home?league={selected.id}&connection=api-key-added", status_code=303)


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    current = _session(request)
    if not current:
        return RedirectResponse("/", status_code=303)
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


@app.get("/league", response_class=HTMLResponse)
def league_page(request: Request, league: str):
    current = _session(request)
    if not current:
        return RedirectResponse("/", status_code=303)
    selected = _league(current, league)
    try:
        context = _league_hq(current, selected)
    except (MFLApiError, ValueError, requests.RequestException) as error:
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
    context.update(
        session=current,
        league=selected,
        active_tool="league",
        error=None,
        activity_rows=activity_rows,
        side_bets=current.side_bets.get(selected.id, []),
    )
    return templates.TemplateResponse(request=request, name="league.html", context=context)


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
    if section not in {"matchup", "pickups", "standings", "block", "ideas"}:
        raise HTTPException(status_code=404)
    client = _client(current, selected)
    context = {"session": current, "league": selected, "section": section, "error": None}
    try:
        if section == "matchup":
            week, _, _, _, matchup = _load_live_scoring_week(client, requested_week=None)
            own = selected.franchise_id.zfill(4)
            if not matchup or not any(team.franchise_id.zfill(4) == own for team in matchup.teams):
                raise ValueError("MFL has no current matchup for your team.")
            context.update(week=week, matchup=matchup, own=own)
        elif section == "pickups":
            week, _, recommendations, _, _ = _load_player_board(client)
            picks = [item for item in recommendations if item.availability.claimable
                     and item.projection is not None and item.roster_delta is not None and item.roster_delta > 0][:5]
            context.update(week=week, picks=picks)
        elif section == "standings":
            rows = client.league_standings()
            details = client.league_details()
            groups = []
            assigned: set[str] = set()
            for division_id, division_name in details.divisions:
                division_rows = [row for row in rows
                                 if details.franchises.get(row["id"])
                                 and details.franchises[row["id"]].division_id == division_id]
                if division_rows:
                    groups.append({"id": division_id, "name": division_name, "rows": division_rows})
                    assigned.update(row["id"] for row in division_rows)
            remaining = [row for row in rows if row["id"] not in assigned]
            if remaining or not groups:
                groups.append({"id": "", "name": "Other teams" if groups else "League standings",
                               "rows": remaining if groups else rows})
            context.update(
                rows=rows,
                groups=groups,
                teams=details.franchises,
                has_divisions=bool(details.divisions),
            )
        elif section == "block":
            block = client.trade_block()
            catalog = client.players()
            own_block = block.get(selected.franchise_id.zfill(4), {"assets": (), "wanted": ""})
            roster = client.trade_rosters().get(selected.franchise_id.zfill(4), set())
            context.update(block=block, catalog=catalog, names=client.franchise_names(), own_block=own_block,
                           players=sorted((catalog[p] for p in roster if p in catalog), key=lambda p:(p.position, p.name)))
        else:
            week = client.current_week()
            if week is None:
                raise ValueError("The current projection week is unavailable.")
            own = selected.franchise_id.zfill(4)
            target = target.zfill(4) if target else ""
            if wanted:
                if not target or target == own or not wanted.isdecimal():
                    raise ValueError("Choose a player from another team to build offers.")
                rosters = {own: client.franchise_roster(own), target: client.franchise_roster(target)}
            else:
                rosters = client.trade_rosters()
            catalog = client.players()
            ids = set().union(*rosters.values()) if rosters else set()
            projections = client.projected_scores(week=week, player_ids=sorted(ids))
            settings = client.lineup_settings()
            ros_weeks = max(0, client.league_details().end_week - week + 1)
            if wanted:
                analysis = analyze_target_trade(own, target, wanted, rosters, catalog, projections, settings, package_size=package_size)
                context.update(analysis=analysis, target=target, ros_weeks=ros_weeks)
            else:
                context["ideas"] = suggest_trades(own, rosters, catalog, projections, settings)
            context.update(week=week, names=client.franchise_names(), ros_weeks=ros_weeks)
        _remember_catalog(current, client)
    except (MFLApiError, ValueError, requests.RequestException) as exc:
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
        week, roster, recommendations, blend, roster_locked = _load_player_board(client)
        _remember_catalog(current, client)
    except MFLApiError as caught:
        log_error("player_board_failed", caught)
        api_error = str(caught)
    positions = sorted(
        {_board_position(item.player) for item in recommendations if item.player.position},
        key=lambda value: (value not in {"QB", "RB", "WR", "TE", "PK", "DEF"}, value),
    )
    nfl_teams = sorted({item.player.team for item in recommendations if item.player.team})
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
            "mfl_projections": blend.mfl_scores,
            "ml_projections": blend.ml_scores,
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
            _load_lineup(client, requested_week=requested_week)
            if requested_week is not None else _load_lineup(client)
        )
        _remember_catalog(current, client)
    except MFLApiError as caught:
        log_error("lineup_load_failed", caught)
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
            "player_scores": getattr(client, "lineup_scores", {}),
            "settings": settings,
            "projection_source": blend.source_label,
            "ml_matched": blend.ml_matched,
            "mfl_projections": blend.mfl_scores,
            "ml_projections": blend.ml_scores,
            "locked_count": sum(
                item.locked for item in recommendation.players
            ) if recommendation else 0,
            "error": api_error,
        },
    )


@app.get("/scores", response_class=HTMLResponse)
def scores_page(request: Request, league: str, week: int | None = None, matchup: str = ""):
    current = _session(request)
    if not current:
        return RedirectResponse("/", status_code=303)
    selected = _league(current, league)
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
                                   **({"matchup_index": matchup_index} if matchup_index is not None else {}))
        )
        _remember_catalog(current, client)
    except (MFLApiError, ValueError) as caught:
        log_error("scores_load_failed", caught)
        api_error = str(caught)
    if selected_week is not None:
        current.selected_week = selected_week
    refresh_state = {"active": False, "next_kickoff": None}
    if selected_week is not None and selected_week == current_week:
        try:
            refresh_state = client.nfl_refresh_state(week=selected_week, now=time.time())
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
    if client.current_week() != week:
        return {"active": False, "next_kickoff": None}
    try:
        return client.nfl_refresh_state(week=week, now=time.time())
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
        result = client.submit_lineup(
            week=preview.week,
            starter_ids=[player.id for player in preview.starters],
        )
        success = f"MFL accepted your Week {preview.week} lineup."
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
        week = client.current_week()
        if week is not None:
            locked = _locked_player_ids(
                [preview.add, preview.drop],
                client.nfl_team_kickoffs(week=week),
            )
            if locked:
                names = ", ".join(
                    player.name
                    for player in (preview.add, preview.drop)
                    if player.id in locked
                )
                raise ValueError(f"Game already started; MFL has locked: {names}")
        result = client.submit_add_drop(preview, replace=preview.replace_existing)
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
        names = {key.zfill(4): value for key, value in client.franchise_names().items()
                 if key.zfill(4) not in {selected.franchise_id.zfill(4), "0000"}}
        if target and target not in names:
            raise ValueError("Choose another team in this league.")
        # Only read the two teams the owner is working with. Loading every
        # league roster made a failed large export block every trade partner.
        own = selected.franchise_id.zfill(4)
        rosters = {own: client.franchise_roster(own)}
        if target:
            rosters[target] = client.franchise_roster(target)
        catalog = client.players()
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
    sessions.pop(session_id or "", None)
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie("wp_session")
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
    live = client.live_scoring(week=week)
    item = next((p for match in live.matchups for team in match.franchises
                 if team.franchise_id == franchise for p in team.players
                 if p.player_id == player_id and p.status.casefold() in {"starter", "s"}), None)
    if item is None:
        raise HTTPException(status_code=409, detail="This player is not a saved starter for this team and week. Reload the matchup.")
    player = client.players().get(player_id)
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
                result["components"] = scoring_components(box, client.scoring_rules(), player.position)
            except MFLApiError as error:
                log_error("scoring_detail_rules_unavailable", error)
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
    player = client.players().get(player_id)
    if not player:
        raise HTTPException(status_code=404, detail="Player not found")
    _remember_catalog(current, client)
    selected_week = week if week is not None else current.selected_week or client.current_week()
    if selected_week is None or not 1 <= selected_week <= 18:
        raise HTTPException(status_code=400, detail="Choose a valid week")
    projection = actual = None
    scoring = []
    try:
        projection = client.projected_scores(week=selected_week, player_ids=[player_id]).get(player_id)
        live = client.live_scoring(week=selected_week)
        actual = next((item.score for matchup in live.matchups for franchise in matchup.franchises for item in franchise.players if item.player_id == player_id), None)
    except MFLApiError as error:
        log_error("player_card_points_unavailable", error)
    try:
        rules = client.scoring_rules().get("positionRules", [])
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
        log_error("player_card_rules_unavailable", error)
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
    week, _, board, blend, _ = _load_player_board(client)
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
