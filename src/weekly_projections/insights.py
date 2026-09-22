from __future__ import annotations

import csv
import math
import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Mapping

import requests

from weekly_projections.mfl.client import MFLPlayer


DEPTH_CHART_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "depth_charts/depth_charts_{year}.csv"
)
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
_DEPTH_TTL = 12 * 60 * 60
_WEATHER_TTL = 30 * 60
_DEPTH_DOWNLOAD_LIMIT = 96_000_000
_DEPTH_SNAPSHOT_ROW_LIMIT = 5_000
_depth_cache: dict[int, tuple[float, str, list[dict[str, str]]]] = {}
_weather_cache: dict[tuple[str, int], tuple[float, "WeatherOutlook"]] = {}


@dataclass(frozen=True)
class DepthRole:
    player_id: str
    role: str
    rank: int | None
    movement: int | None = None


@dataclass(frozen=True)
class WeatherOutlook:
    team: str
    venue_team: str
    label: str
    temperature_f: float | None = None
    precipitation_percent: float | None = None
    wind_mph: float | None = None
    kickoff: int | None = None
    roof: str = "outdoor"

    @property
    def caution(self) -> bool:
        return bool(
            (self.precipitation_percent or 0) >= 45
            or (self.wind_mph or 0) >= 18
            or (self.temperature_f is not None and self.temperature_f <= 25)
        )


@dataclass(frozen=True)
class AccuracyMetric:
    source: str
    position: str
    samples: int
    mae: float
    rmse: float
    bias: float
    weight: float = 0.0


@dataclass(frozen=True)
class RankMetric:
    position: str
    samples: int
    top_half_accuracy: float


@dataclass(frozen=True)
class ProjectionReference:
    player_id: str
    estimate: float
    low: float | None
    high: float | None
    mfl_weight: float
    ml_weight: float


@dataclass(frozen=True)
class ProjectionAccuracyReport:
    weeks: tuple[int, ...]
    metrics: tuple[AccuracyMetric, ...]
    espn_metrics: tuple[RankMetric, ...]
    references: tuple[ProjectionReference, ...]


_TEAM_ALIASES = {
    "GBP": "GB", "JAC": "JAX", "KCC": "KC", "LAR": "LA", "LVR": "LV",
    "NEP": "NE", "NOS": "NO", "SFO": "SF", "TBB": "TB",
}


def canonical_team(value: str) -> str:
    team = str(value or "").strip().upper()
    return _TEAM_ALIASES.get(team, team)


def position_bucket(value: str) -> str:
    position = str(value or "").strip().upper().replace("D/ST", "DEF")
    if position in {"DST", "DEF"}:
        return "DEF"
    if position == "PK":
        return "K"
    return position


def _depth_chart_lines(response) -> Iterable[str]:
    """Yield a bounded decoded CSV stream without retaining the season file."""
    total = 0
    iterator = getattr(response, "iter_lines", None)
    if callable(iterator):
        source = iterator(decode_unicode=False)
    else:
        source = response.content.splitlines()
    for raw_line in source:
        if isinstance(raw_line, bytes):
            total += len(raw_line) + 1
            line = raw_line.decode("utf-8-sig" if total == len(raw_line) + 1 else "utf-8")
        else:
            line = str(raw_line)
            total += len(line.encode("utf-8")) + 1
        if total > _DEPTH_DOWNLOAD_LIMIT:
            raise RuntimeError("nflverse depth chart response exceeded the safe download limit")
        yield line


def _download_depth_chart(year: int) -> tuple[str, list[dict[str, str]]]:
    cached = _depth_cache.get(year)
    now = time.monotonic()
    if cached and now - cached[0] < _DEPTH_TTL:
        return cached[1], cached[2]
    try:
        response = requests.get(
            DEPTH_CHART_URL.format(year=year),
            timeout=(3.05, 45),
            headers={"User-Agent": "WeeklyProjectionsML/0.7 (+personal MFL client)"},
            stream=True,
        )
        response.raise_for_status()
    except requests.RequestException as error:
        raise RuntimeError("nflverse depth charts are temporarily unavailable") from error
    try:
        reader = csv.DictReader(_depth_chart_lines(response))
        if not reader.fieldnames or not {"team", "player_name"}.issubset(reader.fieldnames):
            raise RuntimeError("nflverse returned an unexpected depth chart format")
        updated = ""
        rows: list[dict[str, str]] = []
        for raw_row in reader:
            row = dict(raw_row)
            row_date = str(row.get("dt") or "")
            if row_date > updated:
                updated, rows = row_date, []
            if row_date == updated:
                rows.append(row)
                if len(rows) > _DEPTH_SNAPSHOT_ROW_LIMIT:
                    raise RuntimeError("nflverse depth chart snapshot exceeded the safe row limit")
    except (UnicodeError, csv.Error, requests.RequestException) as error:
        raise RuntimeError("nflverse returned an unreadable depth chart") from error
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()
    if not rows:
        raise RuntimeError("nflverse returned an unexpected depth chart format")
    _depth_cache[year] = (now, updated, rows)
    return updated, rows


def depth_chart_roles(
    players: Iterable[MFLPlayer],
    *,
    year: int,
    previous: Mapping[str, int] | None = None,
) -> tuple[dict[str, DepthRole], str, dict[str, int]]:
    """Match nflverse's current chart and detect changes seen during this session."""
    player_list = list(players)
    updated, rows = _download_depth_chart(year)
    by_espn = {player.espn_id: player for player in player_list if player.espn_id}
    by_name_team = {
        ("".join(character for character in player.name.casefold() if character.isalnum()), canonical_team(player.team)): player
        for player in player_list
    }
    result: dict[str, DepthRole] = {}
    snapshot: dict[str, int] = {}
    previous = previous or {}
    for row in rows:
        player = by_espn.get(str(row.get("espn_id") or ""))
        if player is None:
            name = "".join(character for character in str(row.get("player_name") or "").casefold() if character.isalnum())
            player = by_name_team.get((name, canonical_team(row.get("team", ""))))
        if player is None:
            continue
        try:
            rank = int(float(str(row.get("pos_rank") or "")))
        except ValueError:
            rank = None
        if rank is not None:
            snapshot[player.id] = rank
        old_rank = previous.get(player.id)
        movement = old_rank - rank if old_rank is not None and rank is not None and old_rank != rank else None
        position_name = str(row.get("pos_name") or row.get("pos_abb") or player.position).strip()
        role = f"{position_name} · {'starter' if rank == 1 else f'depth {rank}' if rank else 'depth unranked'}"
        result[player.id] = DepthRole(player.id, role, rank, movement)
    return result, updated, snapshot


# Stadium coordinates are intentionally coarse: they select a local forecast grid,
# not a precise device location. Indoor/covered venues never trigger weather advice.
_STADIUMS: dict[str, tuple[float, float, str]] = {
    "ARI": (33.528, -112.263, "retractable"), "ATL": (33.755, -84.401, "retractable"),
    "BAL": (39.278, -76.623, "outdoor"), "BUF": (42.774, -78.787, "outdoor"),
    "CAR": (35.226, -80.853, "outdoor"), "CHI": (41.862, -87.617, "outdoor"),
    "CIN": (39.095, -84.516, "outdoor"), "CLE": (41.506, -81.699, "outdoor"),
    "DAL": (32.748, -97.093, "retractable"), "DEN": (39.744, -105.020, "outdoor"),
    "DET": (42.340, -83.046, "indoor"), "GB": (44.501, -88.062, "outdoor"),
    "HOU": (29.685, -95.411, "retractable"), "IND": (39.760, -86.164, "retractable"),
    "JAX": (30.324, -81.638, "outdoor"), "KC": (39.049, -94.484, "outdoor"),
    "LA": (33.953, -118.339, "covered"), "LV": (36.091, -115.184, "indoor"),
    "MIA": (25.958, -80.239, "outdoor"), "MIN": (44.974, -93.258, "indoor"),
    "NE": (42.091, -71.264, "outdoor"), "NO": (29.951, -90.081, "indoor"),
    "NYG": (40.813, -74.074, "outdoor"), "NYJ": (40.813, -74.074, "outdoor"),
    "PHI": (39.901, -75.168, "outdoor"), "PIT": (40.447, -80.016, "outdoor"),
    "SEA": (47.595, -122.332, "outdoor"), "SF": (37.403, -121.970, "outdoor"),
    "TB": (27.976, -82.503, "outdoor"), "TEN": (36.166, -86.771, "outdoor"),
    "WAS": (38.908, -76.864, "outdoor"),
}


def _weather_label(code: int) -> str:
    if code == 0:
        return "Clear"
    if code <= 3:
        return "Cloudy"
    if code in {45, 48}:
        return "Fog"
    if code <= 67 or code in {80, 81, 82}:
        return "Rain"
    if code <= 77 or code in {85, 86}:
        return "Snow"
    if code >= 95:
        return "Thunderstorms"
    return "Mixed conditions"


def weather_for_roster(
    players: Iterable[MFLPlayer],
    games: Mapping[str, Mapping[str, object]],
) -> dict[str, WeatherOutlook]:
    result: dict[str, WeatherOutlook] = {}
    needs: dict[tuple[str, int], tuple[str, int, float, float, str]] = {}
    for player in players:
        team = canonical_team(player.team)
        raw = games.get(player.team.upper()) or games.get(team) or {}
        try:
            kickoff = int(raw.get("kickoff") or 0)
        except (TypeError, ValueError):
            kickoff = 0
        opponent = canonical_team(str(raw.get("opponent_team") or ""))
        is_home = str(raw.get("opponent") or "").strip().startswith("vs")
        venue = team if is_home else opponent
        stadium = _STADIUMS.get(venue)
        if not kickoff or not stadium:
            continue
        lat, lon, roof = stadium
        if roof in {"indoor", "covered"}:
            result[team] = WeatherOutlook(team, venue, "Indoor / covered venue", kickoff=kickoff, roof=roof)
            continue
        key = (venue, kickoff)
        cached = _weather_cache.get(key)
        if cached and time.monotonic() - cached[0] < _WEATHER_TTL:
            result[team] = cached[1]
        else:
            needs[key] = (team, kickoff, lat, lon, roof)
    if not needs:
        return result
    values = list(needs.values())
    try:
        response = requests.get(
            WEATHER_URL,
            params={
                "latitude": ",".join(str(item[2]) for item in values),
                "longitude": ",".join(str(item[3]) for item in values),
                "hourly": "temperature_2m,precipitation_probability,weather_code,wind_speed_10m",
                "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
                "timezone": "UTC", "forecast_days": 16,
            },
            timeout=(3.05, 15),
            headers={"User-Agent": "WeeklyProjectionsML/0.7 (+personal MFL client)"},
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as error:
        raise RuntimeError("stadium weather is temporarily unavailable") from error
    payloads = payload if isinstance(payload, list) else [payload]
    if len(payloads) != len(values):
        raise RuntimeError("weather provider returned an unexpected response")
    for item, forecast in zip(values, payloads):
        team, kickoff, _, _, roof = item
        if not isinstance(forecast, dict) or not isinstance(forecast.get("hourly"), dict):
            continue
        hourly = forecast["hourly"]
        times = hourly.get("time") or []
        target = datetime.fromtimestamp(kickoff, timezone.utc).replace(tzinfo=None)
        parsed = []
        for index, raw_time in enumerate(times):
            try:
                parsed.append((abs((datetime.fromisoformat(str(raw_time)) - target).total_seconds()), index))
            except ValueError:
                continue
        if not parsed:
            continue
        index = min(parsed)[1]
        def number(field: str) -> float | None:
            try:
                value = hourly.get(field, [])[index]
                return round(float(value), 1)
            except (IndexError, TypeError, ValueError):
                return None
        try:
            code = int(hourly.get("weather_code", [])[index])
        except (IndexError, TypeError, ValueError):
            code = -1
        venue = next(key[0] for key, value in needs.items() if value == item)
        label = _weather_label(code)
        if roof == "retractable":
            label += " · roof status unknown"
        outlook = WeatherOutlook(
            team, venue, label, number("temperature_2m"),
            number("precipitation_probability"), number("wind_speed_10m"), kickoff, roof,
        )
        _weather_cache[(venue, kickoff)] = (time.monotonic(), outlook)
        result[team] = outlook
    return result


def _scale_ml(
    players: Mapping[str, MFLPlayer],
    mfl: Mapping[str, float],
    ml: Mapping[str, float],
) -> dict[str, float]:
    ratios: dict[str, float] = {}
    positions = {player_id: position_bucket(player.position) for player_id, player in players.items()}
    for position in set(positions.values()):
        paired = [
            mfl[player_id] / ml[player_id]
            for player_id in players
            if positions[player_id] == position and player_id in mfl and ml.get(player_id, 0) > 0
        ]
        if paired:
            ratios[position] = statistics.median(paired)
    return {
        player_id: value * ratios.get(positions.get(player_id, ""), 1.0)
        for player_id, value in ml.items() if player_id in players
    }


def _top_half_accuracy(predicted: Mapping[str, float], actual: Mapping[str, float]) -> tuple[int, int]:
    ids = [player_id for player_id in predicted if player_id in actual]
    if len(ids) < 4:
        return 0, 0
    count = max(1, len(ids) // 2)
    predicted_top = set(sorted(ids, key=lambda item: predicted[item])[:count])
    actual_top = set(sorted(ids, key=lambda item: actual[item], reverse=True)[:count])
    return len(predicted_top & actual_top), count


def evaluate_projection_accuracy(
    players: Mapping[str, MFLPlayer],
    history: Iterable[tuple[int, Mapping[str, float], Mapping[str, float], Mapping[str, float], Mapping[str, float]]],
    *,
    current_mfl: Mapping[str, float],
    current_ml: Mapping[str, float],
    current_ids: Iterable[str],
) -> ProjectionAccuracyReport:
    errors: dict[tuple[str, str], list[float]] = {}
    rank_hits: dict[str, list[int]] = {}
    weeks = []
    for week, mfl, ml, espn, actual in history:
        weeks.append(week)
        scaled_ml = _scale_ml(players, mfl, ml)
        for source, values in (("MFL", mfl), ("StatHead ML · scaled", scaled_ml)):
            for player_id, projected in values.items():
                player = players.get(player_id)
                if player is None or player_id not in actual:
                    continue
                errors.setdefault((source, position_bucket(player.position)), []).append(float(projected) - float(actual[player_id]))
        for position in {position_bucket(player.position) for player in players.values()}:
            ids = {player_id for player_id, player in players.items() if position_bucket(player.position) == position}
            hits, possible = _top_half_accuracy(
                {player_id: espn[player_id] for player_id in ids if player_id in espn},
                {player_id: actual[player_id] for player_id in ids if player_id in actual},
            )
            if possible:
                rank_hits.setdefault(position, []).extend([1] * hits + [0] * (possible - hits))
    raw_metrics: dict[tuple[str, str], AccuracyMetric] = {}
    for key, residuals in errors.items():
        if not residuals:
            continue
        source, position = key
        raw_metrics[key] = AccuracyMetric(
            source, position, len(residuals),
            round(statistics.mean(abs(value) for value in residuals), 2),
            round(math.sqrt(statistics.mean(value * value for value in residuals)), 2),
            round(statistics.mean(residuals), 2),
        )
    metrics = []
    for key, metric in raw_metrics.items():
        other_source = "StatHead ML · scaled" if metric.source == "MFL" else "MFL"
        other = raw_metrics.get((other_source, metric.position))
        if metric.samples >= 5 and other and other.samples >= 5:
            inverse = 1 / max(metric.mae, .25)
            other_inverse = 1 / max(other.mae, .25)
            weight = inverse / (inverse + other_inverse)
        else:
            weight = 1.0 if metric.source == "MFL" else 0.0
        metrics.append(AccuracyMetric(
            metric.source, metric.position, metric.samples, metric.mae, metric.rmse, metric.bias, round(weight, 3),
        ))
    metric_map = {(item.source, item.position): item for item in metrics}
    scaled_current_ml = _scale_ml(players, current_mfl, current_ml)
    references = []
    for player_id in current_ids:
        player = players.get(player_id)
        if player is None:
            continue
        position = position_bucket(player.position)
        mfl_value, ml_value = current_mfl.get(player_id), scaled_current_ml.get(player_id)
        mfl_metric = metric_map.get(("MFL", position))
        ml_metric = metric_map.get(("StatHead ML · scaled", position))
        if mfl_value is None and ml_value is None:
            continue
        if mfl_value is not None and ml_value is not None and mfl_metric and ml_metric:
            mfl_weight, ml_weight = mfl_metric.weight, ml_metric.weight
            estimate = mfl_weight * mfl_value + ml_weight * ml_value
            error = mfl_weight * mfl_metric.mae + ml_weight * ml_metric.mae
            low, high = max(0.0, estimate - 1.28 * error), estimate + 1.28 * error
        elif mfl_value is not None:
            mfl_weight, ml_weight, estimate = 1.0, 0.0, mfl_value
            low = max(0.0, estimate - 1.28 * mfl_metric.mae) if mfl_metric else None
            high = estimate + 1.28 * mfl_metric.mae if mfl_metric else None
        else:
            mfl_weight, ml_weight, estimate = 0.0, 1.0, float(ml_value)
            low = max(0.0, estimate - 1.28 * ml_metric.mae) if ml_metric else None
            high = estimate + 1.28 * ml_metric.mae if ml_metric else None
        references.append(ProjectionReference(
            player_id, round(estimate, 1), round(low, 1) if low is not None else None,
            round(high, 1) if high is not None else None, round(mfl_weight, 2), round(ml_weight, 2),
        ))
    espn_metrics = tuple(
        RankMetric(position, len(values), round(100 * statistics.mean(values), 1))
        for position, values in sorted(rank_hits.items()) if values
    )
    return ProjectionAccuracyReport(
        tuple(sorted(set(weeks))),
        tuple(sorted(metrics, key=lambda item: (item.position, item.source))),
        espn_metrics,
        tuple(references),
    )
