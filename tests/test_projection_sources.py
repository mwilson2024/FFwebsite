from __future__ import annotations

import pytest

from weekly_projections.mfl.client import MFLPlayer
from weekly_projections.projection_sources import (
    blend_projection_scores,
    espn_weekly_ranks,
    stathead_weekly_scores,
)


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
