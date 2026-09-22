from __future__ import annotations

import pytest

from weekly_projections.mfl.client import MFLPlayer
from weekly_projections.projection_sources import (
    blend_projection_scores,
    cbs_weekly_projection_ranks,
    combined_position_ranks,
    espn_weekly_ranks,
    fantasypros_weekly_ranks,
    stathead_weekly_scores,
)


def test_combined_rank_averages_source_order_within_position_only() -> None:
    players = [
        MFLPlayer("a", "Alpha", "WR", "DET"),
        MFLPlayer("b", "Bravo", "WR", "GB"),
        MFLPlayer("c", "Charlie", "WR", "MIN"),
        MFLPlayer("rb", "Runner", "RB", "BUF"),
    ]
    ranks = combined_position_ranks(
        players,
        mfl_scores={"a": 20, "b": 15, "c": 10, "rb": 30},
        ml_scores={"b": 21, "a": 18, "c": 9},
        espn_ranks={"a": 4, "c": 9, "b": 12, "rb": 1},
    )
    assert ranks == {"a": 1.3, "b": 2.0, "c": 2.7, "rb": 1.0}


class FakeResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self):
        return {
            "generatedAt": "2026-09-09T12:00:00Z",
            "players": [
                {
                    "name": "Patrick Mahomes",
                    "pos": "QB",
                    "team": "KC",
                    "wk": [22.5, 21.0],
                },
                {
                    "name": "KC DST",
                    "pos": "DST",
                    "team": "KC",
                    "wk": [8.0, 7.0],
                },
                {
                    "name": "Antoine Winfield Jr.",
                    "pos": "DB",
                    "team": "TB",
                    "wk": [9.0, 8.0],
                },
            ],
        }


class FakeSession:
    def get(self, *args, **kwargs) -> FakeResponse:
        return FakeResponse()


def test_stathead_matches_mfl_names_teams_defenses_and_idp(monkeypatch) -> None:
    from weekly_projections import projection_sources

    monkeypatch.setattr(projection_sources, "_stathead_cache", {})
    players = [
        MFLPlayer("qb", "Mahomes, Patrick", "QB", "KCC"),
        MFLPlayer("dst", "Chiefs, Kansas City", "Def", "KCC"),
        MFLPlayer("db", "Winfield, Antoine Jr.", "S", "TBB"),
    ]
    scores, generated_at = stathead_weekly_scores(
        players, year=2026, week=1, session=FakeSession()
    )
    assert scores == {"qb": 22.5, "dst": 8.0, "db": 9.0}
    assert generated_at == "2026-09-09T12:00:00Z"


def test_ml_projection_is_position_scaled_before_conservative_blend() -> None:
    players = [
        MFLPlayer("one", "One", "WR", "BUF"),
        MFLPlayer("two", "Two", "WR", "MIA"),
        MFLPlayer("three", "Three", "QB", "SEA"),
    ]
    scores = blend_projection_scores(
        players,
        mfl_scores={"one": 10.0, "two": 20.0},
        ml_scores={"one": 5.0, "two": 10.0, "three": 12.0},
    )
    assert scores["one"] == pytest.approx(10.0)
    assert scores["two"] == pytest.approx(20.0)
    assert scores["three"] == pytest.approx(12.0)


class ESPNResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self):
        return {"players": [
            {
                "player": {
                    "id": 123,
                    "fullName": "Patrick Mahomes",
                    "proTeamId": 12,
                    "defaultPositionId": 1,
                    "rankings": {"2": [
                        {"rankSourceId": 4, "rankType": "PPR", "rank": 3},
                        {"rankSourceId": 0, "rankType": "PPR", "rank": 0, "averageRank": 2.5},
                    ]},
                },
            },
            {
                "player": {
                    "id": 999,
                    "fullName": "Kansas City Chiefs D/ST",
                    "proTeamId": 12,
                    "defaultPositionId": 16,
                    "rankings": {"2": [
                        {"rankSourceId": 0, "rankType": "PPR", "rank": 0, "averageRank": 7},
                    ]},
                },
            },
        ]}


class ESPNSession:
    def __init__(self) -> None:
        self.request = None

    def get(self, *args, **kwargs):
        self.request = (args, kwargs)
        return ESPNResponse()


def test_espn_weekly_ranks_use_free_header_feed_and_match_mfl(monkeypatch) -> None:
    from weekly_projections import projection_sources

    monkeypatch.setattr(projection_sources, "_espn_cache", {})
    session = ESPNSession()
    ranks = espn_weekly_ranks(
        [
            MFLPlayer("qb", "Mahomes, Patrick", "QB", "KCC", espn_id="123"),
            MFLPlayer("dst", "Chiefs, Kansas City", "Def", "KCC"),
        ],
        year=2026,
        week=2,
        rank_type="PPR",
        session=session,
    )
    assert ranks == {"qb": 2.5, "dst": 7.0}
    args, kwargs = session.request
    assert "leaguedefaults/1" in args[0]
    assert kwargs["params"] == {"scoringPeriodId": 2, "view": "kona_player_info"}
    assert '"filterRanksForScoringPeriodIds":{"value":[2]}' in kwargs["headers"]["X-Fantasy-Filter"]
    assert "api" not in " ".join(kwargs["headers"]).lower()


class HTMLResponse:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


class HTMLSession:
    def __init__(self, html: str) -> None:
        self.html = html
        self.calls = []

    def get(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return HTMLResponse(self.html)


def test_fantasypros_uses_full_table_and_private_cookie(monkeypatch) -> None:
    from weekly_projections import projection_sources

    monkeypatch.setattr(projection_sources, "_fantasypros_cache", {})
    monkeypatch.setenv("WP_FANTASYPROS_SESSION_COOKIE", "session=private")
    session = HTMLSession('''<script>ecrData = {"year":"2026","week":"3","players":[
        {"player_name":"Josh Jacobs","player_team_id":"GB","player_position_id":"RB","rank_ecr":1},
        {"player_name":"Jahmyr Gibbs","player_team_id":"DET","player_position_id":"RB","rank_ecr":2}
    ]};</script>''')
    ranks = fantasypros_weekly_ranks(
        [MFLPlayer("one", "Jacobs, Josh", "RB", "GBP")],
        year=2026, week=3, session=session,
    )
    assert ranks == {"one": 1.0}
    assert session.calls[0][1]["headers"]["Cookie"] == "session=private"


def test_cbs_projection_points_become_position_ranks(monkeypatch) -> None:
    from weekly_projections import projection_sources

    monkeypatch.setattr(projection_sources, "_cbs_cache", {})
    session = HTMLSession("""
        <h1>Week 3 Proj</h1><table>
        <tr><th>Player</th><th>GP</th><th>Fantasy Points</th><th>FPPG</th></tr>
        <tr><td>J. Allen QB BUF Josh Allen QB BUF</td><td>1</td><td>28.4</td><td>28.4</td></tr>
        <tr><td>P. Mahomes QB KC Patrick Mahomes QB KC</td><td>1</td><td>28.5</td><td>28.5</td></tr>
        </table>
    """)
    ranks = cbs_weekly_projection_ranks(
        [MFLPlayer("allen", "Allen, Josh", "QB", "BUF"),
         MFLPlayer("mahomes", "Mahomes, Patrick", "QB", "KCC")],
        year=2026, week=3, session=session,
    )
    assert ranks == {"mahomes": 1.0, "allen": 2.0}
