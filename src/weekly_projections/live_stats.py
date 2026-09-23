"""Public box-score details; MFL remains authoritative for fantasy totals.

This client deliberately never receives the authenticated MFL session.
Only supported, observable stat/rule combinations are calculated. Differences
are exposed, not assigned to invented touchdowns or scoring adjustments.
"""
from __future__ import annotations

import re
import time
import unicodedata
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from urllib.parse import urlsplit

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


def _normalized_words(value: str) -> str:
    plain = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    return " ".join(re.findall(r"[a-z0-9]+", plain.casefold()))


def _player_display_name(player: MFLPlayer) -> str:
    """Convert MFL's usual ``Last, First`` form to the display order ESPN uses."""
    if "," not in player.name:
        return player.name.strip()
    last, first = (part.strip() for part in player.name.split(",", 1))
    return f"{first} {last}".strip()


def _safe_espn_link(value: object) -> str:
    try:
        parsed = urlsplit(str(value or ""))
    except ValueError:
        return ""
    host = (parsed.hostname or "").casefold()
    if parsed.scheme != "https" or not (host == "espn.com" or host.endswith(".espn.com")):
        return ""
    return parsed.geturl()


def _safe_espn_thumbnail(value: object) -> str:
    try:
        parsed = urlsplit(str(value or ""))
    except ValueError:
        return ""
    return parsed.geturl() if parsed.scheme == "https" and parsed.hostname == "a.espncdn.com" else ""


def _video_link(video: object) -> str:
    if not isinstance(video, dict):
        return ""
    links = video.get("links") if isinstance(video.get("links"), dict) else {}
    web = links.get("web") if isinstance(links.get("web"), dict) else {}
    return _safe_espn_link(web.get("href") or video.get("href") or video.get("url"))


def _video_title(video: object) -> str:
    if not isinstance(video, dict):
        return ""
    return str(video.get("headline") or video.get("title") or video.get("description") or "").strip()


def _touchdown_matches_player(play: dict, player: MFLPlayer) -> bool:
    scoring = play.get("scoringType") if isinstance(play.get("scoringType"), dict) else {}
    play_type = play.get("type") if isinstance(play.get("type"), dict) else {}
    if not (
        str(scoring.get("name", "")).casefold() == "touchdown"
        or str(scoring.get("abbreviation", "")).upper() == "TD"
        or str(play_type.get("abbreviation", "")).upper() == "TD"
        or "touchdown" in str(play_type.get("text", "")).casefold()
    ):
        return False
    if player.position.upper() in {"DEF", "DST", "D/ST"}:
        team = TEAM_ALIASES.get(player.team.upper(), player.team.upper())
        play_team = play.get("team") if isinstance(play.get("team"), dict) else {}
        kind = str(play_type.get("text", "")).casefold()
        return play_team.get("abbreviation") == team and any(
            marker in kind for marker in ("interception", "fumble", "return", "blocked")
        )
    athlete_ids = set()
    for involved in play.get("athletesInvolved") or []:
        if not isinstance(involved, dict):
            continue
        athlete = involved.get("athlete") if isinstance(involved.get("athlete"), dict) else involved
        if athlete.get("id") not in (None, ""):
            athlete_ids.add(str(athlete["id"]))
    if player.espn_id and player.espn_id in athlete_ids:
        return True
    display_name = _normalized_words(_player_display_name(player))
    return bool(display_name and display_name in _normalized_words(play.get("text", "")))


def parse_touchdown_clips(
    payload: dict, player: MFLPlayer, year: int, week: int, event_id: str,
) -> dict | None:
    """Return ESPN scoring-play cards and direct clip links when ESPN publishes them.

    ESPN does not attach a video to every scoring play. In that case the play is
    still shown, with a clearly-labelled link to that game's official highlight
    page instead of inventing an individual clip URL.
    """
    header = payload.get("header", {})
    season = header.get("season", {}) if isinstance(header, dict) else {}
    if season.get("year") != year or season.get("type") != 2 or header.get("week") != week:
        return None
    game_url = f"https://www.espn.com/nfl/video?gameId={event_id}"
    all_videos = [item for item in payload.get("videos") or [] if isinstance(item, dict)]
    used_links: set[str] = set()
    cards = []
    player_name = _player_display_name(player)
    player_words = _normalized_words(player_name)
    last_name = player_words.split()[-1] if player_words else ""
    for play in payload.get("scoringPlays") or []:
        if not isinstance(play, dict) or not _touchdown_matches_player(play, player):
            continue
        nested = play.get("video") or play.get("videos") or []
        candidates = [nested] if isinstance(nested, dict) else list(nested) if isinstance(nested, list) else []
        play_id = str(play.get("id") or "")
        play_text = str(play.get("text") or "Touchdown").strip()
        yardage_match = re.search(r"\b(\d+)\s*(?:yd|yard)", play_text.casefold())
        yardage = yardage_match.group(1) if yardage_match else ""
        exact = []
        descriptive = []
        for video in all_videos:
            relation = str(video.get("playId") or video.get("play_id") or video.get("playID") or "")
            title_words = _normalized_words(_video_title(video))
            if relation and relation == play_id:
                exact.append(video)
            elif last_name and last_name in title_words and ("touchdown" in title_words or " td " in f" {title_words} "):
                if not yardage or re.search(rf"\b{re.escape(yardage)}\b", title_words):
                    descriptive.append(video)
        candidates.extend(exact)
        candidates.extend(descriptive)
        selected_video = next(
            (video for video in candidates if _video_link(video) and _video_link(video) not in used_links),
            None,
        )
        clip_url = _video_link(selected_video)
        if clip_url:
            used_links.add(clip_url)
        period = play.get("period") if isinstance(play.get("period"), dict) else {}
        clock = play.get("clock") if isinstance(play.get("clock"), dict) else {}
        team = play.get("team") if isinstance(play.get("team"), dict) else {}
        cards.append({
            "play_id": play_id,
            "title": _video_title(selected_video) or play_text,
            "description": play_text,
            "period": int(period.get("number") or 0),
            "clock": str(clock.get("displayValue") or ""),
            "team": str(team.get("abbreviation") or player.team),
            "thumbnail_url": _safe_espn_thumbnail(selected_video.get("thumbnail")) if isinstance(selected_video, dict) else "",
            "clip_url": clip_url or game_url,
            "direct_clip": bool(clip_url),
        })
    return {
        "player": player_name,
        "event_id": event_id,
        "game_url": game_url,
        "plays": cards,
        "source": "ESPN",
    }


def weekly_touchdown_clips(player: MFLPlayer, year: int, week: int) -> dict | None:
    """Load touchdown cards lazily for one player and week."""
    if not player.espn_id.isdecimal() and player.position.upper() not in {"DEF", "DST", "D/ST"}:
        return None
    bucket = int(time.monotonic() // 60)
    board = _public_json("scoreboard", (("dates", year), ("seasontype", 2), ("week", week)), bucket)
    team_code = TEAM_ALIASES.get(player.team.upper(), player.team.upper())
    for event in board.get("events", []):
        if event.get("week", {}).get("number") != week or event.get("season", {}).get("year") != year or event.get("season", {}).get("type") != 2:
            continue
        competitors = [team for competition in event.get("competitions", []) for team in competition.get("competitors", [])]
        if not any(team.get("team", {}).get("abbreviation") == team_code for team in competitors):
            continue
        event_id = str(event.get("id", ""))
        if event_id.isdecimal():
            return parse_touchdown_clips(
                _public_json("summary", (("event", event_id),), bucket), player, year, week, event_id,
            )
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
