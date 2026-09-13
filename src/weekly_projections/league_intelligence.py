from __future__ import annotations

from dataclasses import dataclass
from collections import Counter, defaultdict
from typing import Iterable, Mapping

from weekly_projections.mfl.client import MFLFantasyGame, MFLTransaction


@dataclass(frozen=True)
class TeamIntelligence:
    franchise_id: str
    name: str
    wins: float
    losses: float
    ties: float
    points_for: float
    points_against: float
    expected_wins: float
    recent_form: float
    power_score: float
    rank: int

    @property
    def luck(self) -> float:
        return round(self.wins - self.expected_wins, 2)

    @property
    def record(self) -> str:
        wins = int(self.wins) if self.wins.is_integer() else self.wins
        losses = int(self.losses) if self.losses.is_integer() else self.losses
        ties = int(self.ties) if self.ties.is_integer() else self.ties
        return f"{wins}-{losses}" + (f"-{ties}" if self.ties else "")

    @property
    def one_liner(self) -> str:
        if self.luck >= 1:
            return "The record is running ahead of the weekly scoring profile."
        if self.luck <= -1:
            return "The points say this team deserves a better record."
        if self.recent_form >= .7:
            return "One of the league's hottest teams over the last three weeks."
        if self.rank <= 3:
            return "Strong scoring and point differential keep this team near the top."
        return "A middle-of-the-pack profile with room for one lineup swing."


@dataclass(frozen=True)
class WaiverTrend:
    player_id: str
    adds: int
    drops: int

    @property
    def net(self) -> int:
        return self.adds - self.drops


@dataclass(frozen=True)
class LeagueRecap:
    week: int | None
    headline: str
    story: str
    awards: tuple[tuple[str, str], ...]


def _scale(values: Mapping[str, float]) -> dict[str, float]:
    if not values:
        return {}
    low, high = min(values.values()), max(values.values())
    if high == low:
        return {key: .5 for key in values}
    return {key: (value - low) / (high - low) for key, value in values.items()}


def build_power_rankings(
    games: Iterable[MFLFantasyGame],
    names: Mapping[str, str],
    *,
    current_week: int,
) -> tuple[TeamIntelligence, ...]:
    """Build transparent, deterministic rankings from completed fantasy weeks.

    Current-week zeroes are deliberately excluded because the schedule export
    does not reliably distinguish a future 0.0 from a final 0.0.
    """
    team_ids = set(names)
    completed = [
        game for game in games
        if game.week < current_week and len(game.team_ids) == len(game.scores)
        and len(game.team_ids) >= 2 and all(score is not None for score in game.scores)
    ]
    if not completed:
        return ()
    for game in completed:
        team_ids.update(game.team_ids)
    wins = defaultdict(float)
    losses = defaultdict(float)
    ties = defaultdict(float)
    points_for = defaultdict(float)
    points_against = defaultdict(float)
    week_scores: dict[int, dict[str, float]] = defaultdict(dict)
    weekly_results: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for game in completed:
        for index, (team_id, raw_score) in enumerate(zip(game.team_ids, game.scores)):
            score = float(raw_score or 0)
            opponents = [float(value or 0) for opponent_index, value in enumerate(game.scores) if opponent_index != index]
            opponent_score = sum(opponents) / len(opponents)
            points_for[team_id] += score
            points_against[team_id] += opponent_score
            week_scores[game.week][team_id] = score
            if score > opponent_score:
                wins[team_id] += 1
                weekly_results[team_id].append((game.week, 1.0))
            elif score < opponent_score:
                losses[team_id] += 1
                weekly_results[team_id].append((game.week, 0.0))
            else:
                ties[team_id] += 1
                weekly_results[team_id].append((game.week, .5))
    expected = defaultdict(float)
    games_played = defaultdict(int)
    for scores in week_scores.values():
        for team_id, score in scores.items():
            comparisons = [other for other_id, other in scores.items() if other_id != team_id]
            if not comparisons:
                continue
            expected[team_id] += (sum(score > other for other in comparisons) + .5 * sum(score == other for other in comparisons)) / len(comparisons)
            games_played[team_id] += 1
    pf_game = {team_id: points_for[team_id] / max(1, games_played[team_id]) for team_id in team_ids}
    diff_game = {team_id: (points_for[team_id] - points_against[team_id]) / max(1, games_played[team_id]) for team_id in team_ids}
    win_rate = {
        team_id: (wins[team_id] + .5 * ties[team_id]) / max(1, wins[team_id] + losses[team_id] + ties[team_id])
        for team_id in team_ids
    }
    recent = {
        team_id: sum(result for _, result in sorted(weekly_results[team_id])[-3:]) / max(1, len(weekly_results[team_id][-3:]))
        for team_id in team_ids
    }
    pf_scaled, diff_scaled, win_scaled, recent_scaled = map(_scale, (pf_game, diff_game, win_rate, recent))
    raw_power = {
        team_id: 100 * (.45 * pf_scaled[team_id] + .25 * diff_scaled[team_id] + .20 * win_scaled[team_id] + .10 * recent_scaled[team_id])
        for team_id in team_ids
    }
    ordered = sorted(team_ids, key=lambda team_id: (-raw_power[team_id], -points_for[team_id], names.get(team_id, team_id).casefold()))
    return tuple(
        TeamIntelligence(
            franchise_id=team_id,
            name=names.get(team_id, f"Franchise {team_id}"),
            wins=wins[team_id], losses=losses[team_id], ties=ties[team_id],
            points_for=round(points_for[team_id], 2), points_against=round(points_against[team_id], 2),
            expected_wins=round(expected[team_id], 2), recent_form=round(recent[team_id], 3),
            power_score=round(raw_power[team_id], 1), rank=index,
        )
        for index, team_id in enumerate(ordered, 1)
    )


def waiver_trends(transactions: Iterable[MFLTransaction]) -> tuple[WaiverTrend, ...]:
    adds: Counter[str] = Counter()
    drops: Counter[str] = Counter()
    for transaction in transactions:
        adds.update(transaction.adds)
        drops.update(transaction.drops)
    players = set(adds) | set(drops)
    return tuple(sorted(
        (WaiverTrend(player_id, adds[player_id], drops[player_id]) for player_id in players),
        key=lambda item: (-item.net, -item.adds, item.player_id),
    ))


def build_recap(
    games: Iterable[MFLFantasyGame],
    names: Mapping[str, str],
    *,
    current_week: int,
) -> LeagueRecap:
    eligible = [game for game in games if game.week < current_week and all(score is not None for score in game.scores)]
    if not eligible:
        return LeagueRecap(None, "The season story starts here", "Completed MFL matchups will power this recap after Week 1.", ())
    week = max(game.week for game in eligible)
    latest = [game for game in eligible if game.week == week]
    performances = [
        (float(score or 0), names.get(team_id, f"Franchise {team_id}"))
        for game in latest for team_id, score in zip(game.team_ids, game.scores)
    ]
    top_score, top_team = max(performances)
    closest = min(latest, key=lambda game: max(game.scores or (0,)) - min(game.scores or (0,)))
    close_names = " vs. ".join(names.get(team_id, f"Franchise {team_id}") for team_id in closest.team_ids)
    low_score, low_team = min(performances)
    awards = (
        ("Team of the week", f"{top_team} · {top_score:.2f}"),
        ("Sicko Award", f"{low_team} · {low_score:.2f}"),
        ("Nail-biter", close_names),
    )
    return LeagueRecap(
        week,
        f"Week {week}: {top_team} set the pace",
        f"{top_team} led the league with {top_score:.2f} points, while {close_names} delivered the week's closest matchup.",
        awards,
    )


def playoff_probability(left: TeamIntelligence | None, right: TeamIntelligence | None) -> tuple[int, int] | None:
    if not left or not right:
        return None
    difference = left.power_score - right.power_score
    left_percent = max(5, min(95, round(50 + difference * .55)))
    return left_percent, 100 - left_percent


def build_playoff_seeds(
    standings: Iterable[Mapping[str, object]],
    division_by_team: Mapping[str, str],
    division_order: Iterable[str],
    *,
    field_size: int = 8,
) -> tuple[str, ...]:
    """Seed division leaders first, then fill the field by league record.

    Division leaders are ranked against one another for seeds 1-3. Remaining
    teams are ranked for seeds 4-8 by record, then points scored. This is a
    local projection and deliberately ignores MFL's published playoff bracket.
    """
    rows = [dict(row) for row in standings if str(row.get("id", ""))]

    def number(value: object) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def key(row: Mapping[str, object]) -> tuple[float, float, float, str]:
        wins = number(row.get("h2hw"))
        losses = number(row.get("h2hl"))
        ties = number(row.get("h2ht"))
        return (-(wins + ties * .5), losses, -number(row.get("pf")), str(row.get("id", "")))

    ordered = sorted(rows, key=key)
    leaders: list[dict[str, object]] = []
    for division_id in division_order:
        match = next((row for row in ordered if division_by_team.get(str(row["id"])) == division_id), None)
        if match is not None:
            leaders.append(match)
    leaders = sorted(leaders, key=key)[:3]
    leader_ids = {str(row["id"]) for row in leaders}
    remaining = [row for row in ordered if str(row["id"]) not in leader_ids]
    return tuple(str(row["id"]) for row in [*leaders, *remaining][:max(2, field_size)])


def build_local_playoff_games(
    seeds: Iterable[str], *, first_playoff_week: int
) -> tuple[MFLFantasyGame, ...]:
    """Build the projected eight-team quarterfinals without MFL bracket data."""
    seeded = tuple(seeds)[:8]
    if len(seeded) < 2:
        return ()
    pair_indexes = ((0, 7), (3, 4), (1, 6), (2, 5))
    games = []
    for left, right in pair_indexes:
        if left < len(seeded) and right < len(seeded):
            games.append(MFLFantasyGame(first_playoff_week, (seeded[left], seeded[right]), (None, None)))
    return tuple(games)


def rest_of_season_pace(weekly_projection: float | None, *, week: int, end_week: int) -> float | None:
    if weekly_projection is None:
        return None
    return round(max(0.0, weekly_projection) * max(0, end_week - week + 1), 1)
