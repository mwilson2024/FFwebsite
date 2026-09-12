from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp

from weekly_projections.mfl.client import (
    MFLInjury,
    MFLLineupRule,
    MFLLineupSettings,
    MFLPlayer,
)


@dataclass(frozen=True)
class LineupPlayerRecommendation:
    player: MFLPlayer
    projection: float | None
    injury: MFLInjury | None
    roster_status: str
    currently_starting: bool
    recommended_start: bool
    locked: bool
    action: str
    tone: str


@dataclass(frozen=True)
class LineupRecommendation:
    players: tuple[LineupPlayerRecommendation, ...]
    current_starters: frozenset[str]
    recommended_starters: frozenset[str]
    current_projection: float
    recommended_projection: float
    projected_gain: float
    used_league_rules: bool


def _position_tokens(rule_name: str) -> set[str]:
    normalized = rule_name.upper().replace("D/ST", "DEF")
    tokens = set(re.findall(r"[A-Z]+", normalized))
    aliases = {
        "K": {"K", "PK"},
        "PK": {"K", "PK"},
        "DEF": {"DEF", "DST"},
        "DST": {"DEF", "DST"},
        "FLEX": {"RB", "WR", "TE"},
        "SUPERFLEX": {"QB", "RB", "WR", "TE"},
        "OP": {"QB", "RB", "WR", "TE"},
        "DP": {"DT", "DE", "DL", "LB", "CB", "S", "DB"},
        "DL": {"DT", "DE", "DL"},
        "DB": {"CB", "S", "DB"},
    }
    expanded: set[str] = set()
    for token in tokens:
        expanded.update(aliases.get(token, {token}))
    return expanded


def _eligible(player: MFLPlayer, rule: MFLLineupRule) -> bool:
    position = player.position.upper().replace("D/ST", "DEF")
    return position in _position_tokens(rule.name)


def lineup_slots(settings: MFLLineupSettings) -> list[dict]:
    """Represent minimum positions plus the league's variable/flex capacity."""
    def label(name, positions):
        if len(positions) > 1 and positions <= {"RB", "WR", "TE"}:
            return "FLEX"
        if len(positions) > 1 and positions <= {"QB", "RB", "WR", "TE"}:
            return "SUPERFLEX"
        return name.upper()
    slots = []
    extra_positions = set()
    for rule in settings.rules:
        positions = _position_tokens(rule.name)
        slots.extend({"label": label(rule.name, positions), "positions": sorted(positions)} for _ in range(rule.minimum))
        if rule.maximum > rule.minimum:
            extra_positions.update(positions)
    for _ in range(max(0, settings.starter_count - len(slots))):
        slots.append({"label": label("FLEX", extra_positions), "positions": sorted(extra_positions)})
    return slots


def assign_lineup_slots(players: Iterable[MFLPlayer], settings: MFLLineupSettings) -> list[tuple[str, MFLPlayer | None]]:
    slots = lineup_slots(settings)
    selected = sorted(players, key=lambda player: player.id)
    assigned: dict[int, MFLPlayer] = {}
    def place(player, seen):
        for index in sorted(range(len(slots)), key=lambda i: (len(slots[i]["positions"]), i)):
            if index in seen or player.position.upper() not in slots[index]["positions"]:
                continue
            seen.add(index)
            if index not in assigned or place(assigned[index], seen):
                assigned[index] = player
                return True
        return False
    overflow = [player for player in selected if not place(player, set())]
    return [(slot["label"], assigned.get(i)) for i, slot in enumerate(slots)] + [("UNASSIGNED", player) for player in overflow]


def _injury_penalty(injury: MFLInjury | None) -> float:
    if not injury:
        return 0.0
    status = injury.status.casefold()
    if any(term in status for term in ("out", "inactive", "suspend", "reserve", "ir")):
        return 1000.0
    if "doubtful" in status:
        return 20.0
    if "questionable" in status:
        return 1.5
    return 0.0


def _solve_lineup(
    players: list[MFLPlayer],
    settings: MFLLineupSettings,
    scores: Mapping[str, float],
    current_starters: set[str],
    required_starters: set[str] | None = None,
    excluded_players: set[str] | None = None,
) -> set[str] | None:
    if not players or not settings.rules:
        return None
    player_count = len(players)
    rule_count = len(settings.rules)
    variable_count = player_count * rule_count
    upper = np.zeros(variable_count)
    objective = np.zeros(variable_count)
    required_starters = required_starters or set()
    excluded_players = excluded_players or set()

    for player_index, player in enumerate(players):
        for rule_index, rule in enumerate(settings.rules):
            variable = player_index * rule_count + rule_index
            if _eligible(player, rule) and player.id not in excluded_players:
                upper[variable] = 1
                stability_bonus = 0.001 if player.id in current_starters else 0.0
                objective[variable] = -(scores.get(player.id, -25.0) + stability_bonus)

    rows: list[np.ndarray] = []
    lower: list[float] = []
    higher: list[float] = []

    for player_index in range(player_count):
        row = np.zeros(variable_count)
        start = player_index * rule_count
        row[start : start + rule_count] = 1
        rows.append(row)
        lower.append(0)
        higher.append(1)

    for rule_index, rule in enumerate(settings.rules):
        row = np.zeros(variable_count)
        row[rule_index::rule_count] = 1
        rows.append(row)
        lower.append(rule.minimum)
        higher.append(rule.maximum)

    for player_id in required_starters:
        player_index = next(
            (index for index, player in enumerate(players) if player.id == player_id),
            None,
        )
        if player_index is None:
            return None
        row = np.zeros(variable_count)
        start = player_index * rule_count
        row[start : start + rule_count] = 1
        rows.append(row)
        lower.append(1)
        higher.append(1)

    rows.append(np.ones(variable_count))
    lower.append(settings.starter_count)
    higher.append(settings.starter_count)

    result = milp(
        c=objective,
        integrality=np.ones(variable_count),
        bounds=Bounds(np.zeros(variable_count), upper),
        constraints=LinearConstraint(np.vstack(rows), np.array(lower), np.array(higher)),
        options={"time_limit": 2.0},
    )
    if not result.success or result.x is None:
        return None
    chosen: set[str] = set()
    for player_index, player in enumerate(players):
        start = player_index * rule_count
        if np.any(result.x[start : start + rule_count] > 0.5):
            chosen.add(player.id)
    return chosen if len(chosen) == settings.starter_count else None


def lineup_is_legal(players: Iterable[MFLPlayer], settings: MFLLineupSettings) -> bool:
    selected = list(players)
    if len(selected) != settings.starter_count:
        return False
    if not settings.rules:
        return True
    selected_ids = {player.id for player in selected}
    assigned = _solve_lineup(
        selected,
        settings,
        scores={player.id: 0.0 for player in selected},
        current_starters=selected_ids,
        required_starters=set(),
        excluded_players=set(),
    )
    return assigned == selected_ids


def recommend_lineup(
    *,
    roster: Iterable[MFLPlayer],
    settings: MFLLineupSettings,
    projections: Mapping[str, float],
    roster_statuses: Mapping[str, str],
    injuries: Mapping[str, MFLInjury],
    locked_player_ids: set[str] | frozenset[str] = frozenset(),
) -> LineupRecommendation:
    roster_players = list(roster)
    eligible = [
        player for player in roster_players if roster_statuses.get(player.id, "R") not in {"IR", "TS"}
    ]
    current = {
        player.id for player in roster_players if roster_statuses.get(player.id, "R") == "S"
    }
    adjusted_scores = {
        player.id: projections.get(player.id, -25.0) - _injury_penalty(injuries.get(player.id))
        for player in eligible
    }
    locked = set(locked_player_ids)
    recommended = _solve_lineup(
        eligible,
        settings,
        adjusted_scores,
        current,
        required_starters=locked & current,
        excluded_players=locked - current,
    )
    used_rules = recommended is not None
    if recommended is None:
        # Never offer an illegal/lock-breaking "best" lineup when the solver fails.
        recommended = set(current)

    rows: list[LineupPlayerRecommendation] = []
    for player in roster_players:
        is_current = player.id in current
        is_recommended = player.id in recommended
        status = roster_statuses.get(player.id, "R")
        is_locked = player.id in locked
        if status in {"IR", "TS"}:
            action, tone = ("Ineligible", "muted")
        elif is_locked and is_current:
            action, tone = ("Locked starter", "locked")
        elif is_locked:
            action, tone = ("Locked bench", "locked")
        elif is_recommended and not is_current:
            action, tone = ("START", "strong")
        elif is_current and not is_recommended:
            action, tone = ("SIT", "danger")
        elif is_recommended:
            action, tone = ("Keep starting", "good")
        else:
            action, tone = ("Keep benched", "muted")
        rows.append(
            LineupPlayerRecommendation(
                player=player,
                projection=projections.get(player.id),
                injury=injuries.get(player.id),
                roster_status=status,
                currently_starting=is_current,
                recommended_start=is_recommended,
                locked=is_locked,
                action=action,
                tone=tone,
            )
        )
    rows.sort(
        key=lambda item: (
            not item.recommended_start,
            -(item.projection if item.projection is not None else -999.0),
            item.player.name.casefold(),
        )
    )
    current_projection = sum(projections.get(player_id, 0.0) for player_id in current)
    recommended_projection = sum(projections.get(player_id, 0.0) for player_id in recommended)
    return LineupRecommendation(
        players=tuple(rows),
        current_starters=frozenset(current),
        recommended_starters=frozenset(recommended),
        current_projection=current_projection,
        recommended_projection=recommended_projection,
        projected_gain=recommended_projection - current_projection,
        used_league_rules=used_rules,
    )
