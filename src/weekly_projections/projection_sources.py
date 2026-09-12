from __future__ import annotations

import re
import statistics
import time
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import requests

from weekly_projections.mfl.client import MFLPlayer


STATHEAD_WEEKLY_URL = (
    "https://www.stathead.app/data/weekly-projections-{year}.json"
)
_CACHE_SECONDS = 15 * 60
_stathead_cache: dict[int, tuple[float, dict[str, Any]]] = {}


class ProjectionSourceError(RuntimeError):
    """A third-party projection source could not be loaded or parsed."""


@dataclass(frozen=True)
class ProjectionBlend:
    scores: dict[str, float]
    mfl_scores: dict[str, float]
    ml_scores: dict[str, float]
    ml_matched: int
    generated_at: str | None = None

    @property
    def source_label(self) -> str:
        if self.mfl_scores:
            return "MFL league scoring · FantasySharks"
        return "No projection feed available"


def _canonical_name(value: str) -> str:
    value = value.strip()
    if "," in value:
        last, first = value.split(",", 1)
        value = f"{first} {last}"
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = value.casefold()
    value = re.sub(r"\b(jr|sr|ii|iii|iv)\b", " ", value)
    return re.sub(r"[^a-z0-9]+", "", value)


def _canonical_team(value: str) -> str:
    aliases = {
        "GBP": "GB",
        "JAC": "JAX",
        "KCC": "KC",
        "LAR": "LA",
        "NEP": "NE",
        "NOS": "NO",
        "SFO": "SF",
        "TBB": "TB",
    }
    team = value.strip().upper()
    return aliases.get(team, team)


def _position_bucket(value: str) -> str:
    position = value.strip().upper().replace("D/ST", "DST")
    aliases = {
        "PK": "K",
        "DEF": "DST",
        "DE": "DL",
        "DT": "DL",
        "CB": "DB",
        "S": "DB",
    }
    return aliases.get(position, position)


def _download_stathead(year: int, session: requests.Session | None = None) -> dict[str, Any]:
    cached = _stathead_cache.get(year)
    now = time.monotonic()
    if cached and now - cached[0] < _CACHE_SECONDS:
        return cached[1]
    try:
        response = (session or requests).get(
            STATHEAD_WEEKLY_URL.format(year=year),
            timeout=(3.05, 12),
            headers={"User-Agent": "WeeklyProjectionsML/0.4 (+personal MFL client)"},
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as error:
        raise ProjectionSourceError("StatHead weekly projections are unavailable") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("players"), list):
        raise ProjectionSourceError("StatHead returned an unexpected projection format")
    _stathead_cache[year] = (now, payload)
    return payload


def stathead_weekly_scores(
    players: Iterable[MFLPlayer],
    *,
    year: int,
    week: int,
    session: requests.Session | None = None,
) -> tuple[dict[str, float], str | None]:
    """Match StatHead's free weekly ML projections to MFL's player catalog."""
    if week < 1 or week > 18:
        return {}, None
    payload = _download_stathead(year, session=session)
    by_name_team: dict[tuple[str, str], float] = {}
    by_name_position: dict[tuple[str, str], float] = {}
    defenses: dict[str, float] = {}
    for row in payload["players"]:
        if not isinstance(row, dict):
            continue
        weeks = row.get("wk")
        if not isinstance(weeks, list) or len(weeks) < week:
            continue
        value = weeks[week - 1]
        if value is None:
            continue
        try:
            score = float(value)
        except (TypeError, ValueError):
            continue
        name = _canonical_name(str(row.get("name", "")))
        team = _canonical_team(str(row.get("team", "")))
        position = _position_bucket(str(row.get("pos", "")))
        if name:
            by_name_team[(name, team)] = score
            by_name_position[(name, position)] = score
        if position == "DST" and team:
            defenses[team] = score

    matched: dict[str, float] = {}
    for player in players:
        name = _canonical_name(player.name)
        team = _canonical_team(player.team)
        position = _position_bucket(player.position)
        score = (
            defenses.get(team)
            if position == "DST"
            else by_name_team.get((name, team))
        )
        if score is None:
            score = by_name_position.get((name, position))
        if score is not None:
            matched[player.id] = score
    generated_at = payload.get("generatedAt")
    return matched, str(generated_at) if generated_at else None


def blend_projection_scores(
    players: Iterable[MFLPlayer],
    *,
    mfl_scores: Mapping[str, float],
    ml_scores: Mapping[str, float],
    mfl_weight: float = 0.7,
) -> dict[str, float]:
    """Blend after position-scaling ML PPR/default points to MFL league scoring."""
    player_list = list(players)
    positions = {player.id: _position_bucket(player.position) for player in player_list}
    ratios: dict[str, float] = {}
    for position in set(positions.values()):
        paired = [
            mfl_scores[player.id] / ml_scores[player.id]
            for player in player_list
            if positions[player.id] == position
            and player.id in mfl_scores
            and ml_scores.get(player.id, 0) > 0
        ]
        if paired:
            ratios[position] = statistics.median(paired)

    blended: dict[str, float] = {}
    for player in player_list:
        mfl = mfl_scores.get(player.id)
        ml = ml_scores.get(player.id)
        if mfl is not None and ml is not None:
            scaled_ml = ml * ratios.get(positions[player.id], 1.0)
            blended[player.id] = round(
                mfl_weight * mfl + (1.0 - mfl_weight) * scaled_ml, 2
            )
        elif mfl is not None:
            blended[player.id] = mfl
        elif ml is not None:
            blended[player.id] = ml
    return blended


def projection_blend(
    players: Iterable[MFLPlayer],
    *,
    year: int,
    week: int,
    mfl_scores: Mapping[str, float],
    session: requests.Session | None = None,
) -> ProjectionBlend:
    player_list = list(players)
    generated_at: str | None = None
    try:
        ml_scores, generated_at = stathead_weekly_scores(
            player_list, year=year, week=week, session=session
        )
    except ProjectionSourceError:
        ml_scores = {}
    # MFL applies each league's exact rules to FantasySharks' raw projected stats.
    # Generic ML point totals cannot reproduce yardage bonuses or scoring tiers.
    # Keep ML as a separate comparison, never fill missing league points with it.
    scores = dict(mfl_scores)
    return ProjectionBlend(
        scores=scores,
        mfl_scores=dict(mfl_scores),
        ml_scores=ml_scores,
        ml_matched=len(ml_scores),
        generated_at=generated_at,
    )
