"""Transparent, non-transactional fantasy decision comparisons."""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Mapping

from weekly_projections.mfl.client import MFLPlayer


@dataclass(frozen=True)
class PlayerOutcome:
    player: MFLPlayer
    floor: float | None
    median: float | None
    ceiling: float | None
    projection: float | None


@dataclass(frozen=True)
class DecisionResult:
    kind: str
    current: PlayerOutcome
    proposed: PlayerOutcome
    weekly_delta: float | None
    rest_of_season_delta: float | None
    bye_consequences: tuple[str, ...]
    playoff_consequences: tuple[str, ...]
    assumptions: tuple[str, ...]


def _outcome(
    player: MFLPlayer,
    projection: float | None,
    recent_scores: tuple[float, ...],
) -> PlayerOutcome:
    sample = tuple(float(value) for value in recent_scores if value is not None)
    center = statistics.median(sample) if sample else projection
    if center is None:
        return PlayerOutcome(player, None, None, None, projection)
    if len(sample) >= 2:
        ordered = sorted(sample)
        low = ordered[max(0, round((len(ordered) - 1) * .20))]
        high = ordered[min(len(ordered) - 1, round((len(ordered) - 1) * .80))]
        floor = min(low, center)
        ceiling = max(high, center)
    else:
        spread = max(2.0, abs(float(center)) * .35)
        floor, ceiling = max(0.0, float(center) - spread), float(center) + spread
    return PlayerOutcome(player, round(floor, 2), round(float(center), 2), round(ceiling, 2), projection)


def simulate_decision(
    *,
    kind: str,
    current: MFLPlayer,
    proposed: MFLPlayer,
    projections: Mapping[str, float],
    recent_scores: Mapping[str, tuple[float, ...]],
    schedule: Mapping[int, Mapping[str, str]],
    current_week: int,
) -> DecisionResult:
    if kind not in {"start-sit", "waiver", "trade"}:
        raise ValueError("Choose a start/sit, waiver, or trade decision")
    current_outcome = _outcome(current, projections.get(current.id), recent_scores.get(current.id, ()))
    proposed_outcome = _outcome(proposed, projections.get(proposed.id), recent_scores.get(proposed.id, ()))
    weekly_delta = (
        round(proposed_outcome.projection - current_outcome.projection, 2)
        if current_outcome.projection is not None and proposed_outcome.projection is not None
        else None
    )
    remaining_regular = max(0, 14 - current_week + 1)
    ros = round(weekly_delta * remaining_regular, 2) if weekly_delta is not None else None
    bye_notes = []
    for label, player in (("Current", current), ("Proposed", proposed)):
        bye = next((week for week in range(max(1, current_week), 15)
                    if len(schedule.get(week, {})) >= 24 and player.team.upper() not in schedule[week]), None)
        bye_notes.append(f"{label}: Week {bye} bye" if bye else f"{label}: no confirmed remaining bye in the loaded schedule")
    playoff_notes = []
    for week in (15, 16, 17):
        games = schedule.get(week, {})
        current_game = games.get(current.team.upper(), "Schedule unavailable")
        proposed_game = games.get(proposed.team.upper(), "Schedule unavailable")
        playoff_notes.append(f"Week {week}: {current.name} {current_game}; {proposed.name} {proposed_game}")
    assumptions = (
        "MFL league-scored weekly projections are used when available; missing projections remain unavailable.",
        "Floor and ceiling use recent MFL scores when supplied, otherwise a clearly heuristic ±35% range (minimum two points).",
        "Rest-of-season impact is a pace extrapolation through Week 14, not a dynasty value or acceptance model.",
        "Bye and playoff context comes from the loaded NFL schedule and does not predict injuries, usage, or weather.",
        "This comparison never changes a lineup, submits a waiver, or sends a trade.",
    )
    return DecisionResult(kind, current_outcome, proposed_outcome, weekly_delta, ros,
                          tuple(bye_notes), tuple(playoff_notes), assumptions)
