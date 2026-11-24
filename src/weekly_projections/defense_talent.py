"""Weekly, descriptive defense strength from completed NFL games only."""
from __future__ import annotations

import csv
import io
import math
import time
from dataclasses import asdict, dataclass

import requests

from weekly_projections.defense_streaming import UNIT_RANKS_2026, _team_code

STATS_URL = "https://github.com/nflverse/nflverse-data/releases/download/stats_team/stats_team_week_{year}.csv"
SCHEDULE_URL = "https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv"
SOURCE_URL = "https://nflreadr.nflverse.com/articles/dictionary_team_stats.html"


@dataclass(frozen=True)
class DefenseRecent:
    rank: float
    run_rank: float
    pass_rank: float
    points_rank: float
    run_allowed: float
    pass_allowed: float
    points_allowed: float
    weeks: tuple[int, ...]
    rank_change: float
    direction: str | None
    previous_games: int
    run_change: float | None
    pass_change: float | None
    points_change: float | None


@dataclass(frozen=True)
class DefenseTalent:
    rank: float
    run_rank: float
    pass_rank: float
    points_rank: float
    run_allowed: float
    pass_allowed: float
    points_allowed: float
    games: int
    through_week: int
    recent: DefenseRecent | None = None


def _number(value: object) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Non-finite NFL team statistic")
    return number


def build_defense_talent(stats: list[dict], schedule: list[dict], *, year: int,
                         before_week: int) -> dict[str, DefenseTalent]:
    """Equal-weight ranks of rush, net pass and total points allowed per game.

    Use the latest contiguous, fully published set of completed regular-season
    weeks. Byes do not count as games; tied values receive average ranks.
    Points are the opponent's final scoreboard total, including return scores.
    This measures team performance, not individual personnel or schedule strength.
    """
    if before_week <= 3:
        return {}
    rows = {}
    for row in stats:
        if int(row["season"]) == year and row.get("season_type") == "REG":
            key = (row["game_id"], _team_code(row["team"]))
            if key in rows:
                raise ValueError("Duplicate NFL team game")
            rows[key] = row
    completed = []
    through = 0
    for week in range(1, min(before_week, 19)):
        games = [g for g in schedule if int(g["season"]) == year
                 and g.get("game_type") == "REG" and int(g["week"]) == week]
        if not games:
            break
        ready = []
        for game in games:
            home, away = _team_code(game["home_team"]), _team_code(game["away_team"])
            try:
                home_score, away_score = _number(game["home_score"]), _number(game["away_score"])
                home_stats, away_stats = rows[(game["game_id"], home)], rows[(game["game_id"], away)]
                pair = []
                for defense, offense, opponent, points in (
                    (home, away_stats, away, away_score), (away, home_stats, home, home_score),
                ):
                    if _team_code(offense["opponent_team"]) != defense:
                        raise ValueError("NFL team opponent does not match schedule")
                    pair.append((defense, _number(offense["rushing_yards"]),
                                 _number(offense["passing_yards"]) - _number(offense["sack_yards_lost"]),
                                 points, week))
                ready.extend(pair)
            except (KeyError, TypeError, ValueError):
                break
        else:
            completed.extend(ready)
            through = week
            continue
        break
    totals = {team: [0., 0., 0., 0] for team in UNIT_RANKS_2026}
    history = {team: [] for team in totals}
    for team, run, passing, points, game_week in completed:
        if team not in totals:
            raise ValueError("Unknown NFL team")
        values = totals[team]
        for index, value in enumerate((run, passing, points)):
            values[index] += value
        values[3] += 1
        history[team].append((run, passing, points, game_week))
    if through < 3 or any(row[3] < 3 for row in totals.values()):
        return {}
    averages = {team: [value / row[3] for value in row[:3]] for team, row in totals.items()}

    def ranks(values):
        return {team: 1 + sum(v < value for v in values.values())
                + (sum(v == value for v in values.values()) - 1) / 2
                for team, value in values.items()}

    def rank_window(window):
        components = [ranks({team: values[i] for team, values in window.items()}) for i in range(3)]
        overall = ranks({team: sum(component[team] for component in components) / 3 for team in totals})
        return overall, components

    overall, components = rank_window(averages)
    recent_averages = {team: [sum(game[i] for game in games[-3:]) / 3 for i in range(3)]
                       for team, games in history.items()}
    recent_overall, recent_components = rank_window(recent_averages)
    result = {}
    for team, games in history.items():
        change = overall[team] - recent_overall[team]
        direction = ("up" if change > 0 else "down" if change < 0 else "steady") if len(games) > 3 else None
        previous = [sum(game[i] for game in games[-6:-3]) / 3 for i in range(3)] if len(games) >= 6 else None
        # Negative deltas mean fewer yards/points allowed. Never fill a bye or
        # an unavailable game with zero, or compare overlapping windows here.
        deltas = [recent_averages[team][i] - previous[i] for i in range(3)] if previous else [None] * 3
        recent = DefenseRecent(
            recent_overall[team], *(component[team] for component in recent_components),
            *recent_averages[team], tuple(game[3] for game in games[-3:]), change,
            direction, 3 if previous else 0, *deltas,
        )
        result[team] = DefenseTalent(overall[team], *(component[team] for component in components),
                                     *averages[team], int(totals[team][3]), through, recent)
    return result


def _csv_rows(url: str) -> list[dict]:
    deadline = time.monotonic() + 25
    with requests.get(url, timeout=(5, 15), stream=True) as response:
        response.raise_for_status()
        data = bytearray()
        for chunk in response.iter_content(64 * 1024):
            data.extend(chunk)
            if len(data) > 8 * 1024 * 1024 or time.monotonic() > deadline:
                raise ValueError("NFL team data exceeded download bounds")
    result = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
    if len(result) > 20_000:
        raise ValueError("NFL team data exceeded row bounds")
    return result


def load_defense_talent(year: int, before_week: int) -> dict[str, dict]:
    if not 2020 <= year <= 2100 or not 4 <= before_week <= 18:
        return {}
    values = build_defense_talent(_csv_rows(STATS_URL.format(year=year)), _csv_rows(SCHEDULE_URL),
                                  year=year, before_week=before_week)
    return {team: asdict(value) for team, value in values.items()}
