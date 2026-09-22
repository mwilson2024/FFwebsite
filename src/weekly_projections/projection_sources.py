from __future__ import annotations

import json
import os
import re
import statistics
import time
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Iterable, Mapping

import requests

from weekly_projections.mfl.client import MFLPlayer


STATHEAD_WEEKLY_URL = (
    "https://www.stathead.app/data/weekly-projections-{year}.json"
)
ESPN_WEEKLY_RANKINGS_URL = (
    "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/"
    "{year}/segments/0/leaguedefaults/{scoring_id}"
)
FANTASYPROS_RANKINGS_URLS = {
    "QB": "https://www.fantasypros.com/nfl/rankings/qb.php",
    "RB": "https://www.fantasypros.com/nfl/rankings/half-point-ppr-rb.php",
    "WR": "https://www.fantasypros.com/nfl/rankings/half-point-ppr-wr.php",
    "TE": "https://www.fantasypros.com/nfl/rankings/half-point-ppr-te.php",
    "K": "https://www.fantasypros.com/nfl/rankings/k.php",
    "DST": "https://www.fantasypros.com/nfl/rankings/dst.php",
}
CBS_WEEKLY_PROJECTIONS_URL = (
    "https://www.cbssports.com/fantasy/football/stats/{position}/{year}/tp/projections/ppr/"
)
_CACHE_SECONDS = 12 * 60 * 60
_stathead_cache: dict[int, tuple[float, dict[str, Any]]] = {}
_espn_cache: dict[tuple[int, int, str], tuple[float, dict[str, Any]]] = {}
_fantasypros_cache: dict[tuple[int, int], tuple[float, dict[str, float]]] = {}
_cbs_cache: dict[tuple[int, int], tuple[float, dict[str, float]]] = {}

_ESPN_TEAM_IDS = {
    1: "ATL", 2: "BUF", 3: "CHI", 4: "CIN", 5: "CLE", 6: "DAL",
    7: "DEN", 8: "DET", 9: "GB", 10: "TEN", 11: "IND", 12: "KC",
    13: "LV", 14: "LA", 15: "MIA", 16: "MIN", 17: "NE", 18: "NO",
    19: "NYG", 20: "NYJ", 21: "PHI", 22: "ARI", 23: "PIT", 24: "LAC",
    25: "SF", 26: "SEA", 27: "TB", 28: "WAS", 29: "CAR", 30: "JAX",
    33: "BAL", 34: "HOU",
}
_ESPN_POSITION_IDS = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DST"}
_NFL_TEAMS = {
    "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN",
    "DET", "GB", "HOU", "IND", "JAX", "KC", "LV", "LA", "LAC", "MIA",
    "MIN", "NE", "NO", "NYG", "NYJ", "PHI", "PIT", "SEA", "SF", "TB",
    "TEN", "WAS",
}


class ProjectionSourceError(RuntimeError):
    """A third-party projection source could not be loaded or parsed."""


@dataclass(frozen=True)
class ProjectionBlend:
    scores: dict[str, float]
    mfl_scores: dict[str, float]
    ml_scores: dict[str, float]
    ml_matched: int
    generated_at: str | None = None
    espn_ranks: dict[str, float] | None = None
    espn_matched: int = 0
    espn_source: str | None = None
    fantasypros_ranks: dict[str, float] | None = None
    fantasypros_matched: int = 0
    fantasypros_source: str | None = None
    cbs_ranks: dict[str, float] | None = None
    cbs_matched: int = 0
    cbs_source: str | None = None
    combined_ranks: dict[str, float] | None = None
    combined_matched: int = 0
    combined_source: str | None = None

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


def _download_espn_rankings(
    year: int,
    week: int,
    rank_type: str,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    scoring_id = 3 if rank_type == "STANDARD" else 1
    cache_key = (year, week, rank_type)
    cached = _espn_cache.get(cache_key)
    now = time.monotonic()
    # ESPN publishes these ranks periodically, so hourly refreshes are sufficient.
    if cached and now - cached[0] < _CACHE_SECONDS:
        return cached[1]
    fantasy_filter = {
        "players": {
            "limit": 2000,
            "offset": 0,
            "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
            "filterRanksForScoringPeriodIds": {"value": [week]},
            "filterRanksForRankTypes": {"value": [rank_type]},
        }
    }
    try:
        response = (session or requests).get(
            ESPN_WEEKLY_RANKINGS_URL.format(year=year, scoring_id=scoring_id),
            params={
                "scoringPeriodId": week,
                "view": "kona_player_info",
            },
            headers={
                "X-Fantasy-Filter": json.dumps(fantasy_filter, separators=(",", ":")),
                "User-Agent": "WeeklyProjectionsML/0.6 (+personal MFL client)",
            },
            timeout=(3.05, 12),
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as error:
        raise ProjectionSourceError("ESPN weekly rankings are unavailable") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("players"), list):
        raise ProjectionSourceError("ESPN returned an unexpected ranking format")
    _espn_cache[cache_key] = (now, payload)
    return payload


def espn_weekly_ranks(
    players: Iterable[MFLPlayer],
    *,
    year: int,
    week: int,
    rank_type: str = "PPR",
    session: requests.Session | None = None,
) -> dict[str, float]:
    """Match ESPN's free weekly consensus ranks to MFL's player catalog."""
    rank_type = rank_type.strip().upper()
    if rank_type not in {"PPR", "STANDARD"}:
        rank_type = "PPR"
    if week < 1 or week > 18:
        return {}
    payload = _download_espn_rankings(year, week, rank_type, session)
    by_espn_id: dict[str, float] = {}
    by_name_team: dict[tuple[str, str], float] = {}
    by_name_position: dict[tuple[str, str], float] = {}
    defenses: dict[str, float] = {}
    for entry in payload["players"]:
        if not isinstance(entry, dict):
            continue
        row = entry.get("player")
        if not isinstance(row, dict):
            continue
        rankings = row.get("rankings")
        week_rankings = rankings.get(str(week), []) if isinstance(rankings, dict) else []
        if isinstance(week_rankings, dict):
            week_rankings = [week_rankings]
        if not isinstance(week_rankings, list):
            continue
        matching = [
            rank for rank in week_rankings
            if isinstance(rank, dict)
            and str(rank.get("rankType", "")).upper() == rank_type
        ]
        consensus = next(
            (
                rank for rank in matching
                if str(rank.get("rankSourceId", "")) == "0"
                and rank.get("averageRank") not in (None, "")
            ),
            None,
        )
        raw_rank = consensus.get("averageRank") if consensus else None
        if raw_rank in (None, ""):
            published: list[float] = []
            for rank in matching:
                try:
                    value = float(rank.get("rank"))
                except (TypeError, ValueError):
                    continue
                if value > 0:
                    published.append(value)
            raw_rank = statistics.mean(published) if published else None
        try:
            weekly_rank = round(float(raw_rank), 1)
        except (TypeError, ValueError):
            continue
        if weekly_rank <= 0:
            continue
        espn_id = row.get("id")
        if espn_id not in (None, ""):
            by_espn_id[str(espn_id)] = weekly_rank
        name = _canonical_name(str(row.get("fullName") or row.get("name") or ""))
        try:
            team = _ESPN_TEAM_IDS.get(int(row.get("proTeamId")), "")
        except (TypeError, ValueError):
            team = ""
        try:
            position = _ESPN_POSITION_IDS.get(int(row.get("defaultPositionId")), "")
        except (TypeError, ValueError):
            position = ""
        if name:
            by_name_team[(name, team)] = weekly_rank
            by_name_position[(name, position)] = weekly_rank
        if position == "DST" and team:
            defenses[team] = weekly_rank

    matched: dict[str, float] = {}
    for player in players:
        position = _position_bucket(player.position)
        team = _canonical_team(player.team)
        weekly_rank = by_espn_id.get(player.espn_id) if player.espn_id else None
        if weekly_rank is None and position == "DST":
            weekly_rank = defenses.get(team)
        if weekly_rank is None:
            name = _canonical_name(player.name)
            weekly_rank = by_name_team.get((name, team))
            if weekly_rank is None:
                weekly_rank = by_name_position.get((name, position))
        if weekly_rank is not None:
            matched[player.id] = weekly_rank
    return matched


class _RankingTableParser(HTMLParser):
    """Small, dependency-free table reader for third-party ranking pages."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._row is not None and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None


def _match_external_ranks(
    players: Iterable[MFLPlayer], rows: Iterable[tuple[str, str, str, float]],
) -> dict[str, float]:
    by_name_team: dict[tuple[str, str], float] = {}
    by_name_position: dict[tuple[str, str], float] = {}
    defenses: dict[str, float] = {}
    for name, team, position, rank in rows:
        name_key = _canonical_name(name)
        team_key = _canonical_team(team)
        position_key = _position_bucket(position)
        if name_key:
            by_name_team[(name_key, team_key)] = rank
            by_name_position[(name_key, position_key)] = rank
        if position_key == "DST" and team_key:
            defenses[team_key] = rank
    matched: dict[str, float] = {}
    for player in players:
        name = _canonical_name(player.name)
        team = _canonical_team(player.team)
        position = _position_bucket(player.position)
        rank = defenses.get(team) if position == "DST" else by_name_team.get((name, team))
        if rank is None:
            rank = by_name_position.get((name, position))
        if rank is not None:
            matched[player.id] = float(rank)
    return matched


def _fantasypros_rows(html: str, position: str) -> list[tuple[str, str, str, float]]:
    """Parse the full rendered ranking table, including authenticated pages."""
    marker = re.search(r"\becrData\s*=\s*", html)
    if marker:
        try:
            payload, _ = json.JSONDecoder().raw_decode(html[marker.end():])
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("players"), list):
            embedded: list[tuple[str, str, str, float]] = []
            for player in payload["players"]:
                if not isinstance(player, dict):
                    continue
                try:
                    rank = float(player.get("rank_ecr"))
                except (TypeError, ValueError):
                    continue
                name = str(player.get("player_name") or "").strip()
                team = str(player.get("player_team_id") or "").strip()
                row_position = str(player.get("player_position_id") or position)
                if name and team and rank > 0:
                    embedded.append((name, team, row_position, rank))
            if embedded:
                return embedded
    parser = _RankingTableParser()
    parser.feed(html)
    rows: list[tuple[str, str, str, float]] = []
    for cells in parser.rows:
        if len(cells) < 2:
            continue
        rank_match = re.search(r"\b(\d+(?:\.\d+)?)\b", cells[0])
        if not rank_match:
            continue
        text = " ".join(cells[1:4])
        team = next((code for code in reversed(re.findall(r"\b[A-Z]{2,3}\b", text)) if code in _NFL_TEAMS), "")
        name = re.sub(r"\s+\b(?:QB|RB|WR|TE|K|DST)\b.*$", "", cells[1]).strip()
        if team:
            name = re.sub(rf"\s+{re.escape(team)}(?:\s+.*)?$", "", name).strip()
        if not name or not team:
            continue
        rows.append((name, team, position, float(rank_match.group(1))))
    return rows


def fantasypros_weekly_ranks(
    players: Iterable[MFLPlayer], *, year: int, week: int,
    session: requests.Session | None = None,
) -> dict[str, float]:
    """Load full FantasyPros tables; an optional login cookie stays server-side."""
    cache_key = (year, week)
    cached = _fantasypros_cache.get(cache_key)
    now = time.monotonic()
    if cached and now - cached[0] < _CACHE_SECONDS:
        return dict(cached[1])
    player_list = list(players)
    positions = {_position_bucket(player.position) for player in player_list}
    cookie = os.getenv("WP_FANTASYPROS_SESSION_COOKIE", "").strip()
    headers = {"User-Agent": "WeeklyProjectionsML/0.8 (+personal fantasy client)"}
    if cookie:
        headers["Cookie"] = cookie
    parsed: list[tuple[str, str, str, float]] = []
    try:
        for position in ("QB", "RB", "WR", "TE", "K", "DST"):
            if position not in positions:
                continue
            response = (session or requests).get(
                FANTASYPROS_RANKINGS_URLS[position], headers=headers, timeout=(3.05, 12),
            )
            response.raise_for_status()
            # These friendly URLs point at the currently published week. Never
            # relabel current ranks as historical ranks when browsing old weeks.
            if (
                f'"year":"{year}"' not in response.text
                or f'"week":"{week}"' not in response.text
            ):
                continue
            parsed.extend(_fantasypros_rows(response.text, position))
    except requests.RequestException as error:
        raise ProjectionSourceError("FantasyPros weekly rankings are unavailable") from error
    matched = _match_external_ranks(player_list, parsed)
    if not matched:
        raise ProjectionSourceError(
            "FantasyPros did not return a readable full ranking table; refresh its server-side session cookie"
        )
    _fantasypros_cache[cache_key] = (now, matched)
    return matched


def _cbs_rows(html: str, position: str) -> list[tuple[str, str, str, float]]:
    parser = _RankingTableParser()
    parser.feed(html)
    projected: list[tuple[str, str, str, float]] = []
    for cells in parser.rows:
        if len(cells) < 3:
            continue
        player_cell = cells[0]
        matches = re.findall(r"(.+?)\s+(QB|RB|WR|TE|K|DST)\s+([A-Z]{2,3})\b", player_cell)
        if not matches:
            continue
        name, _, team = max(matches, key=lambda item: len(item[0].strip()))
        try:
            points = float(cells[-2])
        except (TypeError, ValueError):
            continue
        projected.append((name.strip(), team, position, points))
    projected.sort(key=lambda row: row[3], reverse=True)
    return [(name, team, pos, float(index)) for index, (name, team, pos, _) in enumerate(projected, 1)]


def cbs_weekly_projection_ranks(
    players: Iterable[MFLPlayer], *, year: int, week: int,
    session: requests.Session | None = None,
) -> dict[str, float]:
    """Convert CBS' public PPR projections to within-position weekly ranks."""
    cache_key = (year, week)
    cached = _cbs_cache.get(cache_key)
    now = time.monotonic()
    if cached and now - cached[0] < _CACHE_SECONDS:
        return dict(cached[1])
    player_list = list(players)
    positions = {_position_bucket(player.position) for player in player_list}
    parsed: list[tuple[str, str, str, float]] = []
    try:
        for position in ("QB", "RB", "WR", "TE", "K", "DST"):
            if position not in positions:
                continue
            response = (session or requests).get(
                CBS_WEEKLY_PROJECTIONS_URL.format(position=position, year=year),
                timeout=(3.05, 12),
                headers={"User-Agent": "WeeklyProjectionsML/0.8 (+personal fantasy client)"},
            )
            response.raise_for_status()
            if f"Week {week} Proj" not in response.text:
                continue
            parsed.extend(_cbs_rows(response.text, position))
    except requests.RequestException as error:
        raise ProjectionSourceError("CBS weekly projections are unavailable") from error
    matched = _match_external_ranks(player_list, parsed)
    if not matched:
        raise ProjectionSourceError("CBS did not return projections for the selected week")
    _cbs_cache[cache_key] = (now, matched)
    return matched


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


def combined_position_ranks(
    players: Iterable[MFLPlayer],
    *,
    mfl_scores: Mapping[str, float],
    ml_scores: Mapping[str, float],
    espn_ranks: Mapping[str, float],
    fantasypros_ranks: Mapping[str, float] | None = None,
    cbs_ranks: Mapping[str, float] | None = None,
) -> dict[str, float]:
    """Equal-weight available source ranks within each fantasy position.

    Point totals from different scoring systems are never averaged. MFL and ML
    points are first converted to position ranks, ESPN's rank is re-ranked within
    the same position, and a player needs at least two covered sources.
    """
    player_list = list(players)

    def source_ranks(values: Mapping[str, float], *, lower_is_better: bool) -> dict[str, float]:
        ranked: dict[str, float] = {}
        positions = {_position_bucket(player.position) for player in player_list}
        for position in positions:
            rows = [
                (player.id, float(values[player.id]))
                for player in player_list
                if _position_bucket(player.position) == position and player.id in values
            ]
            rows.sort(key=lambda row: row[1], reverse=not lower_is_better)
            previous = None
            display_rank = 0
            for index, (player_id, value) in enumerate(rows, 1):
                if previous is None or value != previous:
                    display_rank = index
                    previous = value
                ranked[player_id] = float(display_rank)
        return ranked

    sources = (
        source_ranks(mfl_scores, lower_is_better=False),
        source_ranks(ml_scores, lower_is_better=False),
        source_ranks(espn_ranks, lower_is_better=True),
        source_ranks(fantasypros_ranks or {}, lower_is_better=True),
        source_ranks(cbs_ranks or {}, lower_is_better=True),
    )
    combined: dict[str, float] = {}
    for player in player_list:
        values = [source[player.id] for source in sources if player.id in source]
        if len(values) >= 2:
            combined[player.id] = round(statistics.mean(values), 1)
    return combined


def projection_blend(
    players: Iterable[MFLPlayer],
    *,
    year: int,
    week: int,
    mfl_scores: Mapping[str, float],
    session: requests.Session | None = None,
    include_espn: bool = False,
    espn_rank_type: str = "PPR",
    include_fantasypros: bool = False,
    include_cbs: bool = False,
) -> ProjectionBlend:
    player_list = list(players)
    generated_at: str | None = None
    try:
        ml_scores, generated_at = stathead_weekly_scores(
            player_list, year=year, week=week, session=session
        )
    except ProjectionSourceError:
        ml_scores = {}
    espn_ranks: dict[str, float] = {}
    espn_source: str | None = None
    espn_rank_type = espn_rank_type.strip().upper()
    if espn_rank_type not in {"PPR", "STANDARD"}:
        espn_rank_type = "PPR"
    if include_espn:
        try:
            espn_ranks = espn_weekly_ranks(
                player_list,
                year=year,
                week=week,
                rank_type=espn_rank_type,
                session=session,
            )
            if espn_ranks:
                espn_source = f"ESPN weekly consensus ({espn_rank_type})"
        except ProjectionSourceError:
            espn_ranks = {}
    fantasypros_ranks: dict[str, float] = {}
    fantasypros_source: str | None = None
    if include_fantasypros:
        try:
            fantasypros_ranks = fantasypros_weekly_ranks(
                player_list, year=year, week=week, session=session,
            )
            if fantasypros_ranks:
                fantasypros_source = "FantasyPros Half-PPR weekly expert consensus"
        except ProjectionSourceError:
            fantasypros_ranks = {}
    cbs_ranks: dict[str, float] = {}
    cbs_source: str | None = None
    if include_cbs:
        try:
            cbs_ranks = cbs_weekly_projection_ranks(
                player_list, year=year, week=week, session=session,
            )
            if cbs_ranks:
                cbs_source = "CBS Sports PPR weekly projection rank"
        except ProjectionSourceError:
            cbs_ranks = {}
    combined_ranks = combined_position_ranks(
        player_list,
        mfl_scores=mfl_scores,
        ml_scores=ml_scores,
        espn_ranks=espn_ranks,
        fantasypros_ranks=fantasypros_ranks,
        cbs_ranks=cbs_ranks,
    )
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
        espn_ranks=espn_ranks,
        espn_matched=len(espn_ranks),
        espn_source=espn_source,
        fantasypros_ranks=fantasypros_ranks,
        fantasypros_matched=len(fantasypros_ranks),
        fantasypros_source=fantasypros_source,
        cbs_ranks=cbs_ranks,
        cbs_matched=len(cbs_ranks),
        cbs_source=cbs_source,
        combined_ranks=combined_ranks,
        combined_matched=len(combined_ranks),
        combined_source=(
            "Equal-weight available MFL / ESPN / FantasyPros / CBS / StatHead position ranks"
            if combined_ranks
            else None
        ),
    )
