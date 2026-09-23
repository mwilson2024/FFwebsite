from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from weekly_projections.mfl.client import MFLFantasyGame, MFLLeagueDetails


@dataclass(frozen=True)
class HistoricalFranchise:
    franchise_id: str
    name: str
    division_id: str
    standing_rank: int | None
    wins: int | None
    losses: int | None
    ties: int | None
    points_for: float | None
    points_against: float | None
    victory_points: float | None


@dataclass(frozen=True)
class HistoricalMatchupTeam:
    week: int
    matchup_index: int
    franchise_id: str
    score: float | None


@dataclass(frozen=True)
class HistoricalSeason:
    season: int
    source_league_id: str
    league_name: str
    start_week: int
    end_week: int
    regular_season_end: int
    franchises: tuple[HistoricalFranchise, ...]
    matchup_teams: tuple[HistoricalMatchupTeam, ...]


def _optional_int(value: Any) -> int | None:
    try:
        return int(float(str(value))) if value not in (None, "", "—") else None
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "", "—") else None
    except (TypeError, ValueError):
        return None


def build_historical_season(
    *,
    season: int,
    source_league_id: str,
    details: MFLLeagueDetails,
    standings: Iterable[dict[str, Any]],
    schedule: Iterable[MFLFantasyGame],
) -> HistoricalSeason:
    """Normalize bounded MFL season reports into database-ready rows."""
    if not 1990 <= int(season) <= 2100 or not str(source_league_id).isdecimal():
        raise ValueError("Historical MFL season identity is invalid")

    standings_by_id: dict[str, tuple[int, dict[str, Any]]] = {}
    for rank, row in enumerate(standings, start=1):
        franchise_id = str(row.get("id") or "").zfill(4)
        if franchise_id.isdecimal() and franchise_id != "0000":
            standings_by_id[franchise_id] = (rank, row)

    franchise_ids = set(details.franchises) | set(standings_by_id)
    franchises: list[HistoricalFranchise] = []
    for franchise_id in sorted(franchise_ids):
        team = details.franchises.get(franchise_id)
        rank, row = standings_by_id.get(franchise_id, (None, {}))
        franchises.append(HistoricalFranchise(
            franchise_id=franchise_id,
            name=team.name if team else f"Franchise {franchise_id}",
            division_id=team.division_id if team else "",
            standing_rank=rank,
            wins=_optional_int(row.get("h2hw")),
            losses=_optional_int(row.get("h2hl")),
            ties=_optional_int(row.get("h2ht")),
            points_for=_optional_float(row.get("pf")),
            points_against=_optional_float(row.get("pa")),
            victory_points=_optional_float(row.get("vp")),
        ))

    matchup_teams: list[HistoricalMatchupTeam] = []
    matchup_number: dict[int, int] = {}
    for game in schedule:
        matchup_number[game.week] = matchup_number.get(game.week, 0) + 1
        index = matchup_number[game.week]
        for offset, franchise_id in enumerate(game.team_ids):
            if not str(franchise_id).isdecimal():
                continue
            score = game.scores[offset] if offset < len(game.scores) else None
            matchup_teams.append(HistoricalMatchupTeam(
                week=game.week,
                matchup_index=index,
                franchise_id=str(franchise_id).zfill(4),
                score=score,
            ))

    return HistoricalSeason(
        season=int(season),
        source_league_id=str(source_league_id),
        league_name=details.name or f"League {source_league_id}",
        start_week=details.start_week,
        end_week=details.end_week,
        regular_season_end=details.last_regular_season_week,
        franchises=tuple(franchises),
        matchup_teams=tuple(matchup_teams),
    )
