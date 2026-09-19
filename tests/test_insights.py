from __future__ import annotations

from types import SimpleNamespace

from weekly_projections import insights
from weekly_projections.mfl.client import MFLPlayer


class Response:
    def __init__(self, *, content=b"", payload=None):
        self.content = content
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_depth_chart_matches_espn_id_and_detects_session_movement(monkeypatch):
    insights._depth_cache.clear()
    payload = (
        "dt,team,player_name,espn_id,gsis_id,pos_grp_id,pos_grp,pos_id,pos_name,pos_abb,pos_slot,pos_rank\n"
        "2026-09-19T12:00:00Z,DET,Amon-Ra St. Brown,4374302,x,1,Offense,1,Wide Receiver,WR,1,1\n"
    ).encode()
    monkeypatch.setattr(insights.requests, "get", lambda *args, **kwargs: Response(content=payload))
    player = MFLPlayer("mfl-1", "St. Brown, Amon-Ra", "WR", "DET", espn_id="4374302")
    roles, updated, snapshot = insights.depth_chart_roles(
        [player], year=2026, previous={"mfl-1": 2},
    )
    assert updated == "2026-09-19T12:00:00Z"
    assert roles["mfl-1"].role == "Wide Receiver · starter"
    assert roles["mfl-1"].movement == 1
    assert snapshot == {"mfl-1": 1}


def test_weather_uses_one_bounded_forecast_and_skips_indoor_venue(monkeypatch):
    insights._weather_cache.clear()
    calls = []
    payload = {"hourly": {
        "time": ["2026-09-20T17:00"], "temperature_2m": [51],
        "precipitation_probability": [60], "weather_code": [61], "wind_speed_10m": [20],
    }}
    monkeypatch.setattr(
        insights.requests, "get",
        lambda *args, **kwargs: calls.append((args, kwargs)) or Response(payload=payload),
    )
    kickoff = 1789923600  # 2026-09-20 17:00 UTC
    rows = insights.weather_for_roster(
        [MFLPlayer("buf", "Buffalo Player", "WR", "BUF"), MFLPlayer("det", "Detroit Player", "WR", "DET")],
        {
            "BUF": {"kickoff": kickoff, "opponent_team": "MIA", "opponent": "vs MIA"},
            "DET": {"kickoff": kickoff, "opponent_team": "MIN", "opponent": "vs MIN"},
        },
    )
    assert len(calls) == 1
    assert rows["BUF"].label == "Rain"
    assert rows["BUF"].caution is True
    assert rows["DET"].label == "Indoor / covered venue"


def test_projection_accuracy_learns_position_weights_without_replacing_mfl():
    players = {str(index): MFLPlayer(str(index), f"Player {index}", "QB", "DET") for index in range(1, 7)}
    actual = {str(index): float(30 - index) for index in range(1, 7)}
    mfl = {player_id: value + 1 for player_id, value in actual.items()}
    ml = {player_id: value + (5 if int(player_id) % 2 else -4) for player_id, value in actual.items()}
    espn = {str(index): float(index) for index in range(1, 7)}
    report = insights.evaluate_projection_accuracy(
        players,
        [(1, mfl, ml, espn, actual)],
        current_mfl=mfl,
        current_ml=ml,
        current_ids={"1", "2"},
    )
    metrics = {item.source: item for item in report.metrics}
    assert metrics["MFL"].samples == 6
    assert metrics["MFL"].weight > metrics["StatHead ML · scaled"].weight
    assert round(metrics["MFL"].weight + metrics["StatHead ML · scaled"].weight, 3) == 1
    assert report.espn_metrics[0].top_half_accuracy == 100
    assert len(report.references) == 2
    assert all(item.low is not None and item.high is not None for item in report.references)
