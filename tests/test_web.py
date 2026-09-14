from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from weekly_projections.lineup import LineupPlayerRecommendation, LineupRecommendation
from weekly_projections.mfl.client import (
    MFLAvailability,
    MFLConfig,
    MFLFranchise,
    MFLLeague,
    MFLLeagueDetails,
    MFLFantasyGame,
    MFLTransaction,
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
from weekly_projections.league_intelligence import build_power_rankings, build_recap, waiver_trends


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


def test_health_endpoint_is_available_without_an_mfl_session() -> None:
    response = TestClient(web_app.app).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


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
    client.get("/")
    response = client.post(
        "/login",
        data={"username": "owner", "password": "secret", "year": "2026", "login_csrf": client.cookies.get("wp_login_csrf")},
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
        PlayerRecommendation(
            player=MFLPlayer("o1", "Other Team Star", "QB", "DET"),
            availability=MFLAvailability("o1", status="rostered"),
            projection=22.0,
            roster_delta=None,
            suggested_drop=None,
            recommendation="Rostered",
            recommendation_tone="muted",
            reason="Rostered by Division Rival. Use Trade Center to build an offer.",
            fantasy_team_id="0002",
            fantasy_team_name="Division Rival",
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
        lambda client, *args, **kwargs: (1, roster, board, blend, {"r1"}),
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
    assert "Other Team Star" in response.text
    assert "Division Rival" in response.text
    assert 'id="nfl-team-filter"' in response.text
    assert 'id="fantasy-team-filter"' in response.text
    assert 'id="projection-filter"' in response.text
    assert 'id="player-sort"' in response.text
    assert 'class="board-filter-panel"' in response.text
    assert 'id="player-result-count"' in response.text
    assert 'id="clear-player-filters"' in response.text
    assert "Blind-bid waiver · FAAB" in response.text
    assert 'name="replace_existing"' in response.text


def test_player_market_keeps_team_defense_and_removes_idp() -> None:
    assert web_app._include_on_player_board(MFLPlayer("def", "Lions Defense", "Def", "DET"))
    assert web_app._board_position(MFLPlayer("def", "Lions Defense", "Def", "DET")) == "DEF"
    for position in ("DE", "DT", "LB", "CB", "S", "DB", "DL", "EDGE"):
        assert not web_app._include_on_player_board(MFLPlayer(position, "IDP", position, "DET"))
    assert web_app._include_on_player_board(MFLPlayer("wr", "Receiver", "WR", "DET"))


def test_player_market_loader_merges_all_rosters_and_free_agents(monkeypatch) -> None:
    players = {
        "mine": MFLPlayer("mine", "My Player", "WR", "DET"),
        "free": MFLPlayer("free", "Free Player", "RB", "GB"),
        "def": MFLPlayer("def", "Detroit Defense", "Def", "DET"),
        "other": MFLPlayer("other", "Other Player", "QB", "MIN"),
        "idp": MFLPlayer("idp", "Defensive Back", "DB", "NYG"),
    }

    class BoardClient:
        config = MFLConfig(2026, "11111", "0001", user_cookie="test")
        session = None
        def __init__(self): self._players = dict(players)

        def roster_ids(self): return {"mine"}
        def free_agents(self): return {"free": MFLAvailability("free"), "def": MFLAvailability("def"), "idp": MFLAvailability("idp")}
        def trade_rosters(self): return {"0001": {"mine"}, "0002": {"other"}}
        def league_details(self): return MFLLeagueDetails((), {
            "0001": MFLFranchise("0001", "My Team"),
            "0002": MFLFranchise("0002", "Other Team"),
        })
        def players(self): return dict(self._players)
        def current_week(self): return 1
        def nfl_team_kickoffs(self, *, week): return {}
        def projected_scores(self, **kwargs): return {key: 10.0 for key in self._players}

    monkeypatch.setattr(
        web_app,
        "projection_blend",
        lambda player_list, **kwargs: ProjectionBlend(kwargs["mfl_scores"], kwargs["mfl_scores"], {}, 0),
    )
    week, roster, board, _, _ = web_app._load_player_board(BoardClient())
    rows = {item.player.id: item for item in board}
    assert week == 1
    assert [player.id for player in roster] == ["mine"]
    assert set(rows) == {"mine", "free", "def", "other"}
    assert rows["other"].fantasy_team_name == "Other Team"
    assert rows["def"].is_claimable is True


def test_player_market_loader_reuses_brief_session_cache(monkeypatch) -> None:
    player = MFLPlayer("free", "Free Player", "RB", "DET")

    class CachedBoardClient:
        config = MFLConfig(2026, "11111", "0001", user_cookie="test")
        session = None

        def __init__(self):
            self._players = {player.id: player}
            self._browser_read_cache = {}
            self.roster_reads = 0

        def roster_ids(self):
            self.roster_reads += 1
            return set()

        def free_agents(self): return {player.id: MFLAvailability(player.id)}
        def trade_rosters(self): return {"0001": set()}
        def league_details(self): return MFLLeagueDetails((), {"0001": MFLFranchise("0001", "My Team")})
        def players(self): return dict(self._players)
        def current_week(self): return 1
        def nfl_team_kickoffs(self, *, week): return {}
        def projected_scores(self, **kwargs): return {player.id: 10.0}

    monkeypatch.setattr(
        web_app,
        "projection_blend",
        lambda player_list, **kwargs: ProjectionBlend(kwargs["mfl_scores"], kwargs["mfl_scores"], {}, 0),
    )
    client = CachedBoardClient()
    first = web_app._load_player_board(client)
    second = web_app._load_player_board(client)
    assert second is first
    assert client.roster_reads == 1


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
        lambda client, *args, **kwargs: (
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
    assert "lineup-3" in response.text


def test_league_hq_renders_intelligence_and_tracks_session_side_bets(monkeypatch) -> None:
    web_app.sessions.clear()
    session_id = "league-session"
    league = MFLLeague("11111", "0001", "Home League")
    web_app.sessions[session_id] = web_app.BrowserSession(
        mfl_cookie="mfl-session-cookie", year=2026, leagues=[league], csrf_token="csrf",
    )
    teams = {
        "0001": MFLFranchise("0001", "Alpha", faab_balance=70, waiver_order=2),
        "0002": MFLFranchise("0002", "Bravo", faab_balance=50, waiver_order=1),
    }
    games = (MFLFantasyGame(1, ("0001", "0002"), (120.0, 100.0)),)
    names = {key: team.name for key, team in teams.items()}
    rankings = build_power_rankings(games, names, current_week=2)
    activity = (MFLTransaction("t1", "WAIVER", 100, ("0001",), ("p1",), ("p2",), bid=3),)
    hq = {
        "details": MFLLeagueDetails((), teams, name="Test HQ", end_week=17),
        "teams": teams, "names": names, "current_week": 2,
        "standings": [{"id":"0001","h2hw":"1","h2hl":"0","h2ht":"0","pf":"120","pa":"100"}],
        "groups": [{"id":"","name":"League standings","rows":[{"id":"0001","h2hw":"1","h2hl":"0","h2ht":"0","pf":"120","pa":"100"}]}],
        "schedule": games, "activity": activity,
        "catalog": {"p1": MFLPlayer("p1", "Pickup", "WR", "DET"), "p2": MFLPlayer("p2", "Drop", "RB", "GB")},
        "rankings": rankings, "rank_by_team": {row.franchise_id: row for row in rankings},
        "recap": build_recap(games, names, current_week=2), "trends": waiver_trends(activity),
        "playoff_games": [], "errors": {},
    }
    monkeypatch.setattr(web_app, "_league_hq", lambda current, selected: dict(hq))
    client = TestClient(web_app.app)
    client.cookies.set("wp_session", session_id)
    response = client.get("/league?league=11111")
    assert response.status_code == 200
    assert "Power rankings &amp; luck index" in response.text
    assert "Waiver wire intelligence" in response.text
    assert "Pickup" in response.text
    assert '<details class="activity-entry">' in response.text
    assert 'id="activity-card"' not in response.text
    assert "WR · DET" in response.text
    assert "Winning FAAB bid" in response.text
    assert "$3" in response.text
    assert "MFL reference" in response.text
    assert 'data-equal-scroll-cards' in response.text
    assert 'data-scroll-height-source' in response.text
    assert 'data-scroll-height-target' in response.text
    assert "/static/league.css?v=4" in response.text
    assert "/static/interface.js?v=4" in response.text
    assert "Side-bet tracker" in response.text
    response = client.post("/league/side-bets", data={
        "league":"11111", "csrf_token":"csrf", "title":"QB duel",
        "participants":"A vs B", "stake":"pizza",
    }, follow_redirects=False)
    assert response.status_code == 303
    assert web_app.sessions[session_id].side_bets["11111"][0].title == "QB duel"
    profile = client.get("/manager/0001?league=11111")
    assert profile.status_code == 200
    assert "FRANCHISE PROFILE" in profile.text
    assert "Alpha" in profile.text
    assert "without inventing results" in profile.text


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
        lambda client, requested_week=None, **kwargs: (
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
    monkeypatch.setattr(web_app, "_load_live_scoring_week", lambda client, requested_week=None, **kwargs: (requested_week, 2, live, {}, head_to_head))
    returned = client.get("/scores?league=11111")
    assert "Week 1 Scores" in returned.text
