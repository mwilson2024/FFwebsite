from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from weekly_projections.lineup import LineupPlayerRecommendation, LineupRecommendation
from weekly_projections.mfl.client import (
    MFLAvailability,
    MFLLeague,
    MFLLiveFranchise,
    MFLLiveMatchup,
    MFLLivePlayer,
    MFLLiveScoring,
    MFLLineupRule,
    MFLLineupSettings,
    MFLPlayer,
)
from weekly_projections.recommendations import PlayerRecommendation
from weekly_projections.projection_sources import ProjectionBlend
from weekly_projections.web import app as web_app


class FakeMFLClient:
    def __init__(self, config) -> None:
        self.config = config

    def login(self) -> None:
        return None

    def account_leagues(self) -> list[MFLLeague]:
        return [
            MFLLeague("11111", "0001", "Home League"),
            MFLLeague("22222", "0002", "Road League"),
        ]

    def user_cookie(self) -> str:
        return "mfl-session-cookie"


def test_login_page_is_local_and_not_cached() -> None:
    web_app.sessions.clear()
    client = TestClient(web_app.app)
    response = client.get("/")
    assert response.status_code == 200
    assert "Connect your account" in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-frame-options"] == "DENY"


def test_login_discovers_and_displays_two_leagues(monkeypatch) -> None:
    web_app.sessions.clear()
    monkeypatch.setattr(web_app, "MFLClient", FakeMFLClient)
    client = TestClient(web_app.app)
    response = client.post(
        "/login",
        data={"username": "owner", "password": "secret", "year": "2026"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Home League" in response.text
    assert "Road League" in response.text
    cookie = client.cookies.get("wp_session")
    assert cookie
    assert web_app.sessions[cookie].mfl_cookie == "mfl-session-cookie"


def test_move_page_shows_full_board_projections_and_locks(monkeypatch) -> None:
    web_app.sessions.clear()
    session_id = "browser-session"
    league = MFLLeague("11111", "0001", "Home League")
    web_app.sessions[session_id] = web_app.BrowserSession(
        mfl_cookie="mfl-session-cookie",
        year=2026,
        leagues=[league],
        csrf_token="csrf",
    )
    roster = [MFLPlayer("r1", "Roster Bench", "WR", "BUF")]
    board = [
        PlayerRecommendation(
            player=MFLPlayer("a1", "Recommended Add", "WR", "MIA"),
            availability=MFLAvailability("a1"),
            projection=14.5,
            roster_delta=4.0,
            suggested_drop=roster[0],
            recommendation="Strong target",
            recommendation_tone="strong",
            reason="Projects 4.0 points above your lowest projected player at this position.",
        ),
        PlayerRecommendation(
            player=MFLPlayer("a2", "Locked Prospect", "RB", "DAL"),
            availability=MFLAvailability("a2", status="locked", locked=True),
            projection=10.0,
            roster_delta=None,
            suggested_drop=None,
            recommendation="Watch list",
            recommendation_tone="muted",
            reason="There is no projected roster player at the same position to compare.",
        ),
    ]
    blend = ProjectionBlend(
        scores={"a1": 14.5, "a2": 10.0},
        mfl_scores={"a1": 14.0},
        ml_scores={"a1": 15.0},
        ml_matched=1,
    )
    monkeypatch.setattr(
        web_app,
        "_load_player_board",
        lambda client: (1, roster, board, blend, {"r1"}),
    )

    client = TestClient(web_app.app)
    client.cookies.set("wp_session", session_id)
    response = client.get("/moves?league=11111")

    assert response.status_code == 200
    assert "Recommended Add" in response.text
    assert "14.5" in response.text
    assert "Strong target" in response.text
    assert "Locked Prospect" in response.text
    assert "Locked" in response.text
    assert 'value="a2"' in response.text
    assert 'value="a2"' in response.text and "disabled" in response.text
    assert "MFL league scoring" in response.text
    assert "Roster Bench · BUF · LOCKED" in response.text


def test_lineup_page_shows_start_sit_recommendations(monkeypatch) -> None:
    web_app.sessions.clear()
    session_id = "lineup-session"
    league = MFLLeague("11111", "0001", "Home League")
    web_app.sessions[session_id] = web_app.BrowserSession(
        mfl_cookie="mfl-session-cookie",
        year=2026,
        leagues=[league],
        csrf_token="csrf",
    )
    player = MFLPlayer("p1", "Recommended Starter", "WR", "MIA")
    recommendation = LineupRecommendation(
        players=(
            LineupPlayerRecommendation(
                player=player,
                projection=14.2,
                injury=None,
                roster_status="NS",
                currently_starting=False,
                recommended_start=True,
                locked=False,
                action="START",
                tone="strong",
            ),
        ),
        current_starters=frozenset(),
        recommended_starters=frozenset({"p1"}),
        current_projection=0,
        recommended_projection=14.2,
        projected_gain=14.2,
        used_league_rules=True,
    )
    settings = MFLLineupSettings(1, (MFLLineupRule("WR", 1, 1),))
    monkeypatch.setattr(
        web_app,
        "_load_lineup",
        lambda client: (
            1,
            recommendation,
            settings,
            ProjectionBlend(
                scores={"p1": 14.2},
                mfl_scores={"p1": 14.0},
                ml_scores={"p1": 14.7},
                ml_matched=1,
            ),
        ),
    )

    client = TestClient(web_app.app)
    client.cookies.set("wp_session", session_id)
    response = client.get("/lineup?league=11111")

    assert response.status_code == 200
    assert "Recommended Starter" in response.text
    assert "14.2" in response.text
    assert "START" in response.text
    assert 'name="starter_ids"' in response.text
    assert 'data-roster-section="starters"' in response.text
    assert 'data-roster-section="bench"' in response.text
    assert 'data-player-card="p1"' in response.text
    assert 'id="use-recommended"' in response.text


def test_kickoff_locks_are_enforced_server_side() -> None:
    players = [
        MFLPlayer("started", "Started Game", "WR", "NEP"),
        MFLPlayer("future", "Future Game", "RB", "SEA"),
    ]
    locked = web_app._locked_player_ids(
        players,
        {"NEP": 100, "SEA": 300},
        now=200,
    )
    assert locked == {"started"}
    with pytest.raises(ValueError, match="has kicked off"):
        web_app._check_locked_players_unchanged(
            selected_ids=set(),
            statuses={"started": "S", "future": "NS"},
            locked_ids=locked,
        )


def test_live_scores_page_renders_mfl_matchup(monkeypatch) -> None:
    web_app.sessions.clear()
    session_id = "scores-session"
    league = MFLLeague("11111", "0001", "Home League")
    web_app.sessions[session_id] = web_app.BrowserSession(
        mfl_cookie="mfl-session-cookie",
        year=2026,
        leagues=[league],
        csrf_token="csrf",
    )
    player = MFLPlayer("p1", "Live Player", "WR", "NEP")
    live_player = MFLLivePlayer("p1", 12.5, "starter", 900)
    live = MFLLiveScoring(
        week=1,
        matchups=(
            MFLLiveMatchup(
                franchises=(
                    MFLLiveFranchise("0001", 42.5, False, 2, 1, 8100, (live_player,)),
                    MFLLiveFranchise("0002", 38.0, True, 3, 0, 10800, ()),
                )
            ),
        ),
    )
    own_player = web_app.LivePlayerView(player, 12.5, "starter", 900, 14.0)
    opponent_player = web_app.LivePlayerView(
        MFLPlayer("p2", "Opponent Player", "RB", "SEA"),
        10.0,
        "starter",
        0,
        11.5,
    )
    head_to_head = web_app.HeadToHeadView(
        teams=(
            web_app.LiveTeamView("0001", "My Team", 42.5, False, 2, 1, (own_player,)),
            web_app.LiveTeamView(
                "0002", "Opponent", 38.0, True, 3, 0, (opponent_player,)
            ),
        )
    )
    monkeypatch.setattr(
        web_app,
        "_load_live_scoring_week",
        lambda client, requested_week: (
            requested_week or 2,
            2,
            live,
            {"0001": "My Team", "0002": "Opponent"},
            head_to_head,
        ),
    )
    client = TestClient(web_app.app)
    client.cookies.set("wp_session", session_id)
    response = client.get("/scores?league=11111&week=1")
    assert response.status_code == 200
    assert "Week 1" in response.text
    assert "Week 2 · Current" in response.text
    assert "View week" in response.text
    assert "My Team" in response.text
    assert "Opponent" in response.text
    assert "Live Player" in response.text
    assert "Opponent Player" in response.text
    assert "14.0" in response.text
    assert "12.50" in response.text
    assert 'class="matchup-player-row"' in response.text
    # An explicit choice survives a return without a week query parameter.
    monkeypatch.setattr(web_app, "_load_live_scoring_week", lambda client, requested_week: (requested_week, 2, live, {}, head_to_head))
    returned = client.get("/scores?league=11111")
    assert "Week 1 Scores" in returned.text
