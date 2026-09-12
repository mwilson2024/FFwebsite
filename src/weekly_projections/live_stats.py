"""Public box-score details; MFL remains authoritative for fantasy totals.

This client deliberately never receives the authenticated MFL session.
Only supported, observable stat/rule combinations are calculated. Differences
are exposed, not assigned to invented touchdowns or scoring adjustments.
"""
from __future__ import annotations

import re
import time
from decimal import Decimal, InvalidOperation
from functools import lru_cache

import requests

from weekly_projections.mfl.client import MFLPlayer

ESPN_API = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
TEAM_ALIASES = {"LVR": "LV", "LAR": "LAR", "JAC": "JAX", "NEP": "NE", "NOS": "NO", "GBP": "GB", "KCC": "KC", "SFO": "SF", "TBB": "TB", "SDC": "LAC", "OAK": "LV", "STL": "LAR"}
EVENTS = {
    "PY": ("passing", "passingYards", "Passing yards"),
    "#P": ("passing", "passingTouchdowns", "Passing TDs"),
    "IN": ("passing", "interceptions", "Interceptions thrown"),
    "RY": ("rushing", "rushingYards", "Rushing yards"),
    "#R": ("rushing", "rushingTouchdowns", "Rushing TDs"),
    "CY": ("receiving", "receivingYards", "Receiving yards"),
    "CC": ("receiving", "receptions", "Receptions"),
    "#C": ("receiving", "receivingTouchdowns", "Receiving TDs"),
    "FL": ("fumbles", "fumblesLost", "Fumbles lost"),
    "EP": ("kicking", "extraPointsMade", "Extra points"),
    "#KT": ("kickReturns", "kickReturnTouchdowns", "Kick return TDs"),
    "#UT": ("puntReturns", "puntReturnTouchdowns", "Punt return TDs"),
    "FC": ("teamDefense", "fumblesRecovered", "Fumbles recovered"),
    "IC": ("teamDefense", "interceptions", "Interceptions caught"),
    "SK": ("teamDefense", "sacks", "Sacks"),
}
STAT_FIELDS = {
    "passing": [("completions/passingAttempts", "C/ATT"), ("passingYards", "pass yds"), ("passingTouchdowns", "pass TD"), ("interceptions", "INT")],
    "rushing": [("rushingAttempts", "carries"), ("rushingYards", "rush yds"), ("rushingTouchdowns", "rush TD")],
    "receiving": [("receptions", "rec"), ("receivingYards", "rec yds"), ("receivingTouchdowns", "rec TD"), ("receivingTargets", "targets")],
    "fumbles": [("fumblesLost", "fumbles lost")],
    "kicking": [("fieldGoalsMade/fieldGoalAttempts", "FG"), ("longFieldGoalMade", "long FG"), ("extraPointsMade/extraPointAttempts", "XP")],
    "defensive": [("totalTackles", "tackles"), ("soloTackles", "solo"), ("sacks", "sacks"), ("defensiveTouchdowns", "def TD")],
    "interceptions": [("interceptions", "INT caught"), ("interceptionTouchdowns", "INT TD")],
    "kickReturns": [("kickReturnYards", "kick ret yds"), ("kickReturnTouchdowns", "kick ret TD")],
    "puntReturns": [("puntReturnYards", "punt ret yds"), ("puntReturnTouchdowns", "punt ret TD")],
}


@lru_cache(maxsize=128)
def _public_json(path: str, params: tuple, minute: int) -> dict:
    # Fixed origin and separately-created request: no MFL cookies or league IDs.
    response = requests.get(f"{ESPN_API}/{path}", params=dict(params), timeout=8)
    response.raise_for_status()
    return response.json()


def parse_boxscore(payload: dict, player: MFLPlayer, year: int, week: int) -> dict | None:
    header = payload.get("header", {})
    season = header.get("season", {})
    if season.get("year") != year or season.get("type") != 2 or header.get("week") != week:
        return None
    competitions = header.get("competitions") or []
    if not competitions:
        return None
    status = competitions[0].get("status", {}).get("type", {})
    if status.get("state") not in {"in", "post"}:
        return None
    categories = {}
    for team in payload.get("boxscore", {}).get("players", []):
        for category in team.get("statistics", []):
            for athlete in category.get("athletes", []):
                if str(athlete.get("athlete", {}).get("id", "")) != player.espn_id or not player.espn_id:
                    continue
                categories[category.get("name", "")] = dict(zip(category.get("keys", []), athlete.get("stats", [])))
    lines = []
    for category, fields in STAT_FIELDS.items():
        values = categories.get(category, {})
        parts = [f"{values[key]} {label}" for key, label in fields if key in values and values[key] not in {"--", "-", ""}]
        if parts:
            lines.append(" · ".join(parts))
    # D/ST has no athlete ID. Show team-game facts, without treating opponent
    # points as fantasy points allowed (MFL can exclude offensive return TDs).
    if player.position.upper() in {"DEF", "DST"}:
        team_code = TEAM_ALIASES.get(player.team.upper(), player.team.upper())
        competitors = competitions[0].get("competitors", [])
        own = next((t for t in competitors if t.get("team", {}).get("abbreviation") == team_code), None)
        other = next((t for t in competitors if t is not own), None) if own else None
        if own and other:
            lines = [f"Game score: {team_code} {own.get('score', '—')} – {other.get('team', {}).get('abbreviation', '')} {other.get('score', '—')}"]
            opponent = next((t for t in payload.get("boxscore", {}).get("teams", [])
                             if str(t.get("team", {}).get("id")) == str(other.get("team", {}).get("id"))), {})
            opponent_stats = {s.get("name"): s.get("displayValue", "") for s in opponent.get("statistics", [])}
            defense = {}
            for source, destination in [("interceptions", "interceptions"), ("fumblesLost", "fumblesRecovered")]:
                if re.fullmatch(r"\d+", opponent_stats.get(source, "")):
                    defense[destination] = opponent_stats[source]
            sacks = re.fullmatch(r"(\d+(?:\.\d+)?)-\d+", opponent_stats.get("sacksYardsLost", ""))
            if sacks:
                defense["sacks"] = sacks[1]
            categories["teamDefense"] = defense
            parts = [f"{defense[key]} {label}" for key, label in [("sacks", "sacks"), ("interceptions", "INT"), ("fumblesRecovered", "fumbles recovered")] if key in defense]
            if parts:
                lines.insert(0, " · ".join(parts))
    if not lines:
        return None
    kicking = categories.get("kicking", {})
    if "extraPointsMade/extraPointAttempts" in kicking:
        kicking["extraPointsMade"] = kicking["extraPointsMade/extraPointAttempts"].split("/")[0]
    return {"state": "Final" if status.get("completed") is True else "Live", "stat_lines": lines, "categories": categories}


def weekly_boxscore(player: MFLPlayer, year: int, week: int) -> dict | None:
    if not player.espn_id.isdecimal() and player.position.upper() not in {"DEF", "DST"}:
        return None
    bucket = int(time.monotonic() // 60)
    board = _public_json("scoreboard", (("dates", year), ("seasontype", 2), ("week", week)), bucket)
    code = TEAM_ALIASES.get(player.team.upper(), player.team.upper())
    for event in board.get("events", []):
        if event.get("week", {}).get("number") != week or event.get("season", {}).get("year") != year or event.get("season", {}).get("type") != 2:
            continue
        competitors = [t for c in event.get("competitions", []) for t in c.get("competitors", [])]
        if not any(t.get("team", {}).get("abbreviation") == code for t in competitors):
            continue
        event_id = str(event.get("id", ""))
        if event_id.isdecimal():
            return parse_boxscore(_public_json("summary", (("event", event_id),), bucket), player, year, week)
    return None


def _text(value) -> str:
    return str(value.get("$t", "")) if isinstance(value, dict) else str(value)


def scoring_components(boxscore: dict, rules: dict, position: str) -> list[dict]:
    """Conservative subset: aggregate count/yard multipliers and flat bonuses.

    Distance-based TD/FG rules, fractional-step formulas and unsupported
    events remain unallocated. Never use eval on MFL rule text.
    """
    groups = rules.get("positionRules", [])
    if isinstance(groups, dict):
        groups = [groups]
    position = {"K": "PK", "DST": "DEF"}.get(position.upper(), position.upper())
    result = []
    for group in groups:
        if position not in _text(group.get("positions", "")).upper().split("|"):
            continue
        items = group.get("rule", [])
        if isinstance(items, dict):
            items = [items]
        for rule in items:
            event = _text(rule.get("event", ""))
            if event not in EVENTS:
                continue
            category, key, label = EVENTS[event]
            raw = boxscore.get("categories", {}).get(category, {}).get(key)
            bounds = re.fullmatch(r"(-?\d+(?:\.\d+)?)-(-?\d+(?:\.\d+)?)", _text(rule.get("range", "")))
            expression = _text(rule.get("points", ""))
            if raw is None or not bounds or not re.fullmatch(r"\*?-?(?:\d+(?:\.\d+)?|\.\d+)", expression):
                continue
            try:
                amount = Decimal(str(raw))
                if not amount.is_finite() or not Decimal(bounds[1]) <= amount <= Decimal(bounds[2]):
                    continue
                rate = Decimal(expression.lstrip("*"))
                points = amount * rate if expression.startswith("*") else rate
            except InvalidOperation:
                continue
            if points:
                result.append({"label": label if expression.startswith("*") else f"{label} bonus ({bounds[1]}–{bounds[2]})", "stat": str(amount), "points": float(points), "rule": expression})
    return result
