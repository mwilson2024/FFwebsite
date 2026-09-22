"""Explainable defense-streaming fit; never a replacement for MFL points."""
from __future__ import annotations

from dataclasses import dataclass
import math
import statistics
from typing import Iterable, Mapping

from weekly_projections.mfl.client import MFLPlayer, MFLTransaction
from weekly_projections.recommendations import PlayerRecommendation


DEFENSE_BID_LOOKBACK_DAYS = 14

TALENT_SOURCE_URL = "https://g.espncdn.com/s/ffldraftkit/26/NFLDK2026_CS_ClayProjections2026.pdf"
TALENT_SOURCE_DATE = "2026-09-09"
# ESPN / Mike Clay, 2026 NFL Unit Grades, page 63. Each pair is
# (offensive talent rank, defensive talent rank); 1 is strongest, 32 weakest.
# A dated seasonal snapshot is intentional: never reuse it for another year.
UNIT_RANKS_2026 = {
    "LAR": (1, 1), "BAL": (5, 4), "PHI": (8, 5), "DET": (9, 8),
    "NE": (10, 7), "BUF": (2, 27), "SEA": (12, 6), "DEN": (11, 9),
    "DAL": (6, 16), "SF": (4, 22), "KC": (7, 13), "CIN": (3, 25),
    "GB": (15, 14), "HOU": (24, 2), "LAC": (13, 18), "TB": (16, 12),
    "CHI": (14, 23), "PIT": (26, 3), "JAX": (18, 21), "MIN": (20, 15),
    "IND": (19, 24), "NYJ": (23, 10), "NYG": (21, 20), "WAS": (17, 30),
    "NO": (25, 17), "ATL": (22, 26), "CAR": (30, 19), "CLE": (31, 11),
    "LV": (28, 29), "TEN": (29, 28), "ARI": (27, 31), "MIA": (32, 32),
}


def _team_code(value: str) -> str:
    aliases = {
        "ARZ": "ARI", "BLT": "BAL", "CLV": "CLE", "GBP": "GB",
        "HST": "HOU", "JAC": "JAX", "KCC": "KC", "LA": "LAR",
        "LVR": "LV", "NEP": "NE", "NOS": "NO", "SFO": "SF",
        "TBB": "TB", "WSH": "WAS",
    }
    team = value.strip().upper()
    return aliases.get(team, team)


@dataclass(frozen=True)
class DefenseStream:
    item: PlayerRecommendation
    opponent: str
    talent_rank: int | None
    offense_rank: int | None
    score: float | None
    label: str
    explanation: str
    league_matchup_rank: int | None = None


def rank_defense_streams(
    board: Iterable[PlayerRecommendation],
    *,
    year: int,
    games: Mapping[str, dict],
    opponent_strength: Mapping[str, dict],
    now: float,
) -> list[DefenseStream]:
    """Compare claimable defenses and the owner's defense using known inputs.

    Core fit is 50% defensive talent + 50% opposing offense weakness. When
    MFL's DEF points-allowed rank is present, use 45/45/10 instead. Scores are
    0-100 model fit, not fantasy points, probabilities, or official ESPN ranks.
    Games already begun and missing inputs get no current-week fit score.
    """
    ranks = UNIT_RANKS_2026 if year == 2026 else {}
    schedule = {_team_code(team): game for team, game in games.items()}
    result = []
    for item in board:
        position = item.player.position.strip().upper().replace("D/ST", "DST")
        if position not in {"DEF", "DST"}:
            continue
        if item.is_rostered and item.market_status != "mine":
            continue
        if not item.is_rostered and not item.is_claimable:
            continue
        team = _team_code(item.player.team)
        game = schedule.get(team, {})
        opponent = _team_code(str(game.get("opponent_team") or ""))
        if not opponent:
            opponent = _team_code(str(game.get("opponent") or "").removeprefix("vs ").removeprefix("@ "))
        talent_rank = ranks[team][1] if team in ranks else None
        offense_rank = ranks[opponent][0] if opponent in ranks else None
        try:
            kickoff = float(game.get("kickoff") or 0)
        except (ValueError, TypeError):
            kickoff = 0
        score = None
        league_rank = None
        if not ranks:
            label, explanation = "Talent data unavailable", "No verified talent snapshot exists for this season."
        elif not opponent:
            label, explanation = "No scheduled opponent", "Bye or unavailable schedule; no streaming score is assigned."
        elif talent_rank is None or offense_rank is None:
            label, explanation = "Incomplete talent data", "A verified team rank is missing; no streaming score is assigned."
        elif game.get("final") or (kickoff > 0 and kickoff <= now):
            label, explanation = "Game already started", "Not a current-week stream. Any available claim is for a later waiver run."
        else:
            defense = (32 - talent_rank) / 31
            weak_offense = (offense_rank - 1) / 31
            score = 100 * (.5 * defense + .5 * weak_offense)
            strength = opponent_strength.get(item.player.id, {})
            try:
                rank, teams = int(strength.get("rank", 0)), int(strength.get("teams", 0))
            except (ValueError, TypeError):
                rank, teams = 0, 0
            if str(strength.get("position", "")).upper() == "DEF" and teams > 1 and 1 <= rank <= teams:
                league_rank = rank
                score = 100 * (.45 * defense + .45 * weak_offense + .1 * (teams - rank) / (teams - 1))
            score = round(score, 1)
            label = "Strong streaming fit" if score >= 65 else "Matchup-dependent" if score >= 40 else "Risky matchup"
            explanation = f"Defense talent #{talent_rank} faces offense #{offense_rank} (1 strongest, 32 weakest)."
            if league_rank is not None:
                explanation += f" MFL defense matchup #{league_rank} also contributes."
            if kickoff <= 0:
                explanation += " Kickoff time unavailable; recheck eligibility before using this defense."
        result.append(DefenseStream(item, opponent, talent_rank, offense_rank, score, label, explanation, league_rank))
    return sorted(result, key=lambda row: (
        row.score is None, -(row.score or 0),
        -(row.item.projection if row.item.projection is not None else -999),
        row.item.player.name.casefold(),
    ))


@dataclass(frozen=True)
class DefenseBid:
    suggested: int | None
    low: int | None
    high: int | None
    explanation: str


def defense_waiver_pricing(
    streams: Iterable[DefenseStream], *, balance: float | None,
    transactions: Iterable[MFLTransaction], catalog: Mapping[str, MFLPlayer],
    now: float,
) -> dict:
    """Advisory whole-unit bids, capped by remaining funds; never an auto-bid.

    Use only single-defense completed BBID awards in the same bounded window
    as League HQ. Multi-add bids cannot safely be allocated to one defense.
    No history means an explicitly labeled 2% budget heuristic. A conservative
    10% remaining-budget ceiling prevents chasing an unaffordable market.
    """
    budget = math.floor(balance) if balance is not None and math.isfinite(balance) and balance >= 0 else None
    history = []
    seen = set()
    for transaction in transactions:
        if transaction.id in seen:
            continue
        seen.add(transaction.id)
        if (transaction.kind.upper() != "BBID_WAIVER" or transaction.bid is None
                or transaction.bid < 0 or len(transaction.adds) != 1
                or transaction.timestamp is None
                or not now - DEFENSE_BID_LOOKBACK_DAYS * 86400 <= transaction.timestamp <= now):
            continue
        player = catalog.get(transaction.adds[0])
        if not player or player.position.strip().upper() not in {"DEF", "DST", "D/ST"}:
            continue
        history.append({"name": player.name, "bid": transaction.bid, "timestamp": transaction.timestamp})
    history.sort(key=lambda entry: entry["timestamp"], reverse=True)
    market_median = statistics.median(entry["bid"] for entry in history) if history else None
    bids = {}
    for stream in streams:
        if stream.item.is_rostered or stream.item.market_status not in {"waiver", "locked"}:
            continue
        if budget is None:
            bid = DefenseBid(None, None, None, "Remaining FAAB unavailable; no bid suggested.")
        elif stream.score is None:
            bid = DefenseBid(None, None, None, "No current-week streaming fit; no matchup-based bid suggested.")
        elif budget == 0:
            bid = DefenseBid(0, 0, 0, "No FAAB left. A zero bid is only usable if your league permits it.")
        else:
            factor = max(.5, min(1.5, stream.score / 60))
            base = market_median if market_median is not None else budget * .02
            ceiling = min(budget, max(1, math.floor(budget * .10)))
            suggested = min(ceiling, max(0, math.floor(base * factor + .5)))
            low = min(suggested, max(0, math.floor(base * factor * .75)))
            high = min(ceiling, max(suggested, math.ceil(base * factor * 1.25)))
            if market_median is None:
                explanation = "No recent defense awards found. Budget-only heuristic: 2% of remaining FAAB, adjusted for fit."
            else:
                explanation = f"Based on {len(history)} recent defense awards (median {market_median:g}), adjusted for fit."
                if len(history) < 3:
                    explanation += " Small sample; low confidence."
            explanation += f" Conservative ceiling {ceiling} (10% of remaining budget, minimum 1). Not a winning-bid prediction."
            bid = DefenseBid(suggested, low, high, explanation)
        bids[stream.item.player.id] = bid
    return {"balance": budget, "history": history[:5], "sample_count": len(history),
            "median": market_median, "bids": bids}
