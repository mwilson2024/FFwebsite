from __future__ import annotations

import csv
import io
import time
from dataclasses import dataclass
from typing import Iterable, Mapping

import requests

from weekly_projections.lineup import assign_lineup_slots
from weekly_projections.mfl.client import MFLLineupSettings, MFLPlayer
from weekly_projections.recommendations import PlayerRecommendation


SCHEDULE_URL = "https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv"
_SCHEDULE_TTL = 7 * 24 * 60 * 60
_schedule_cache: dict[int, tuple[float, dict[int, dict[str, str]]]] = {}
_TEAM_ALIASES = {
    "GBP": "GB", "JAC": "JAX", "KCC": "KC", "LAR": "LA", "LVR": "LV",
    "NEP": "NE", "NOS": "NO", "SFO": "SF", "TBB": "TB",
}


def _team(value: str) -> str:
    normalized = str(value or "").strip().upper()
    return _TEAM_ALIASES.get(normalized, normalized)


def _position(value: str) -> str:
    normalized = str(value or "").strip().upper().replace("D/ST", "DEF")
    return "DEF" if normalized == "DST" else "PK" if normalized == "K" else normalized


@dataclass(frozen=True)
class PlannerWeek:
    week: int
    is_playoff: bool
    bye_players: tuple[MFLPlayer, ...]
    missing_slots: tuple[str, ...]
    schedule_available: bool


@dataclass(frozen=True)
class PlannerPlayer:
    player: MFLPlayer
    projection: float | None
    weeks: tuple[tuple[int, str, str], ...]


@dataclass(frozen=True)
class StashCandidate:
    player: MFLPlayer
    projection: float | None
    reason: str
    drop: MFLPlayer | None
    kind: str


@dataclass(frozen=True)
class RosterPlan:
    weeks: tuple[int, ...]
    week_rows: tuple[PlannerWeek, ...]
    player_rows: tuple[PlannerPlayer, ...]
    stashes: tuple[StashCandidate, ...]
    weaknesses: tuple[PlayerRecommendation, ...]


def load_nfl_schedule(year: int) -> dict[int, dict[str, str]]:
    """Load the public NFL schedule once, without spending MFL request budget."""
    cached = _schedule_cache.get(year)
    now = time.monotonic()
    if cached and now - cached[0] < _SCHEDULE_TTL:
        return cached[1]
    try:
        response = requests.get(
            SCHEDULE_URL,
            timeout=(3.05, 25),
            headers={"User-Agent": "WeeklyProjectionsML/0.8 (+personal MFL client)"},
        )
        response.raise_for_status()
    except requests.RequestException as error:
        raise RuntimeError("nflverse's NFL schedule is temporarily unavailable") from error
    if len(response.content) > 15_000_000:
        raise RuntimeError("nflverse's NFL schedule response was unexpectedly large")
    try:
        rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
    except (UnicodeError, csv.Error) as error:
        raise RuntimeError("nflverse returned an unreadable NFL schedule") from error
    schedule: dict[int, dict[str, str]] = {}
    for row in rows:
        try:
            season = int(str(row.get("season") or "0"))
            week = int(str(row.get("week") or "0"))
        except ValueError:
            continue
        if season != year or not 1 <= week <= 18 or str(row.get("game_type") or "REG") != "REG":
            continue
        home, away = _team(row.get("home_team", "")), _team(row.get("away_team", ""))
        if not home or not away:
            continue
        schedule.setdefault(week, {})[home] = f"vs {away}"
        schedule[week][away] = f"@ {home}"
    if not schedule:
        raise RuntimeError("nflverse has not published this season's NFL schedule")
    _schedule_cache[year] = (now, schedule)
    return schedule


def build_roster_plan(
    *,
    roster: Iterable[MFLPlayer],
    settings: MFLLineupSettings,
    projections: Mapping[str, float],
    schedule: Mapping[int, Mapping[str, str]],
    current_week: int,
    recommendations: Iterable[PlayerRecommendation],
    depth_ranks: Mapping[str, int] | None = None,
) -> RosterPlan:
    roster = tuple(roster)
    candidates = tuple(
        item for item in recommendations
        if not item.is_rostered and item.is_claimable
    )
    weeks = sorted(set(range(current_week, min(18, current_week + 5) + 1)) | {
        week for week in (15, 16, 17) if week >= current_week
    })
    week_rows = []
    for week in weeks:
        games = schedule.get(week, {})
        complete = len(games) >= 24
        bye_players = tuple(
            player for player in roster
            if complete and _team(player.team) and _team(player.team) not in games
        )
        active = [player for player in roster if player not in bye_players]
        assigned = assign_lineup_slots(active, settings)
        missing = tuple(label for label, player in assigned if player is None)
        week_rows.append(PlannerWeek(week, week in {15, 16, 17}, bye_players, missing, complete))

    player_rows = []
    for player in sorted(roster, key=lambda item: (_position(item.position), item.name.casefold())):
        cells = []
        team = _team(player.team)
        for week in weeks:
            games = schedule.get(week, {})
            if len(games) < 24:
                label, tone = "Schedule unavailable", "unknown"
            elif team not in games:
                label, tone = "BYE", "bye"
            else:
                label, tone = games[team], "playoff" if week in {15, 16, 17} else "game"
            cells.append((week, label, tone))
        player_rows.append(PlannerPlayer(player, projections.get(player.id), tuple(cells)))

    weaknesses = tuple(sorted(
        (item for item in candidates if item.roster_delta is not None and item.roster_delta > 0),
        key=lambda item: (-(item.roster_delta or 0), -(item.projection or 0)),
    )[:5])

    depth_ranks = depth_ranks or {}
    roster_by_team_position = {(_team(player.team), _position(player.position)) for player in roster}
    stash_rows: list[StashCandidate] = []
    used: set[str] = set()
    for week_row in week_rows:
        for bye_player in week_row.bye_players:
            match = next((
                item for item in sorted(candidates, key=lambda row: -(row.projection or -999))
                if item.player.id not in used
                and _position(item.player.position) == _position(bye_player.position)
                and _team(item.player.team) in schedule.get(week_row.week, {})
            ), None)
            if match:
                used.add(match.player.id)
                stash_rows.append(StashCandidate(
                    match.player, match.projection,
                    f"Covers {bye_player.name} in Week {week_row.week}.",
                    match.suggested_drop, "Bye-week stash",
                ))
    for item in sorted(candidates, key=lambda row: -(row.projection or -999)):
        rank = depth_ranks.get(item.player.id)
        if (item.player.id in used or _position(item.player.position) != "RB"
                or rank not in {2, 3}
                or (_team(item.player.team), "RB") not in roster_by_team_position):
            continue
        used.add(item.player.id)
        stash_rows.append(StashCandidate(
            item.player, item.projection,
            f"Depth-chart RB{rank} behind a backfield already on your roster.",
            item.suggested_drop, "Handcuff",
        ))
        if len(stash_rows) >= 6:
            break
    return RosterPlan(
        tuple(weeks), tuple(week_rows), tuple(player_rows),
        tuple(stash_rows[:6]), weaknesses,
    )
