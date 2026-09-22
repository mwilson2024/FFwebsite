from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from weekly_projections.lineup import LineupPlayerRecommendation, LineupRecommendation
from weekly_projections.mfl.client import (
    AddDropPreview,
    MFLAvailability,
    MFLApiError,
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
    MFLMessageThread,
    MFLChatMessage,
    MFLPlayer,
    MFLWriteUncertainError,
)
from weekly_projections.recommendations import PlayerRecommendation
from weekly_projections.projection_sources import ProjectionBlend
from weekly_projections.web import app as web_app
from weekly_projections.league_intelligence import build_power_rankings, build_recap, waiver_trends
from weekly_projections.insights import AccuracyMetric, RankMetric, ProjectionAccuracyReport


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


def test_pwa_manifest_worker_and_offline_shell_do_not_cache_private_pages() -> None:
    client = TestClient(web_app.app)
    manifest = client.get("/manifest.webmanifest")
    assert manifest.status_code == 200
    assert manifest.json()["display"] == "standalone"
    assert {item["sizes"] for item in manifest.json()["icons"]} == {"192x192", "512x512"}
    assert any(item["short_name"] == "Score" for item in manifest.json()["shortcuts"])
    worker = client.get("/service-worker.js")
    assert worker.status_code == 200
    assert worker.headers["service-worker-allowed"] == "/"
    assert "event.request.mode === 'navigate'" in worker.text
    assert "cache.put" not in worker.text
    offline = client.get("/offline")
    assert offline.status_code == 200
    assert "private league pages are never saved" in offline.text


def test_pwa_score_shortcut_uses_remembered_league(monkeypatch) -> None:
    web_app.sessions.clear()
    league = MFLLeague("11111", "0001", "Home League")
    web_app.sessions["pwa-score-session"] = web_app.BrowserSession("cookie", 2026, [league], "csrf")
    client = TestClient(web_app.app)
    client.cookies.set("wp_session", "pwa-score-session")
    response = client.get("/dashboard?source=pwa-score", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/scores?league=11111"


def test_roster_planner_page_renders_schedule_gaps_and_stashes(monkeypatch) -> None:
    web_app.sessions.clear()
    league = MFLLeague("11111", "0001", "Home League")
    web_app.sessions["planner-session"] = web_app.BrowserSession("cookie", 2026, [league], "csrf")
    quarterback = MFLPlayer("qb", "Starting QB", "QB", "DET")
    runner = MFLPlayer("rb", "Starting RB", "RB", "BUF")
    candidate = MFLPlayer("qb2", "Bye Cover", "QB", "KC")
    board = [PlayerRecommendation(
        candidate, MFLAvailability("qb2", status="waiver", locked=True), 18, 2,
        quarterback, "Waiver target", "good", "Upgrade",
    )]
    blend = ProjectionBlend(scores={"qb":20, "rb":12, "qb2":18}, mfl_scores={}, ml_scores={}, ml_matched=0)

    class PlannerClient:
        config = MFLConfig(2026, "11111", "0001")
        def lineup_settings(self):
            return MFLLineupSettings(2, (MFLLineupRule("QB", 1, 1), MFLLineupRule("RB", 1, 1)))

    schedule = {}
    for week in (2, 3, 4, 5, 6, 7, 15, 16, 17):
        games = {f"T{index}": "vs X" for index in range(24)}
        games.update({"BUF":"vs MIA", "KC":"@ LV"})
        if week != 2:
            games["DET"] = "@ GB"
        schedule[week] = games
    monkeypatch.setattr(web_app, "_client", lambda *args: PlannerClient())
    monkeypatch.setattr(web_app, "_load_player_board", lambda *args: (2, [quarterback, runner], board, blend, set()))
    monkeypatch.setattr(web_app, "load_nfl_schedule", lambda year: schedule)
    monkeypatch.setattr(web_app, "depth_chart_roles", lambda *args, **kwargs: ({}, "", {}))
    client = TestClient(web_app.app)
    client.cookies.set("wp_session", "planner-session")
    response = client.get("/planner?league=11111")
    assert response.status_code == 200
    assert "Multi-week roster planner" in response.text
    assert "Possible open slots: QB" in response.text
    assert "Bye Cover" in response.text
    assert "Week 15" in response.text


def test_move_page_renders_budget_safe_waiver_queue(monkeypatch) -> None:
    web_app.sessions.clear()
    league = MFLLeague("11111", "0001", "Home League")
    current = web_app.BrowserSession("cookie", 2026, [league], "csrf")
    web_app.sessions["queue-session"] = current
    drop = MFLPlayer("drop", "Bench Receiver", "WR", "BUF")
    add = MFLPlayer("add", "Waiver Receiver", "WR", "DET")
    board = [PlayerRecommendation(
        add, MFLAvailability("add", status="waiver", locked=True), 15, 4,
        drop, "Waiver target", "good", "Projected upgrade",
    )]
    current.player_catalog = {"drop": drop, "add": add}

    class QueueClient:
        config = MFLConfig(2026, "11111", "0001")
        week_games = {}
        opponent_strength = {}
        def league_details(self):
            return MFLLeagueDetails((), {"0001": MFLFranchise("0001", "My Team", faab_balance=25)})
        def transactions(self, **kwargs): return ()

    monkeypatch.setattr(web_app, "_client", lambda *args: QueueClient())
    monkeypatch.setattr(web_app, "_remember_catalog", lambda *args: None)
    monkeypatch.setattr(web_app, "_load_player_board", lambda *args: (
        2, [drop], board, ProjectionBlend(scores={"add":15}, mfl_scores={}, ml_scores={}, ml_matched=0), set(),
    ))
    client = TestClient(web_app.app)
    client.cookies.set("wp_session", "queue-session")
    response = client.get("/moves?league=11111")
    assert response.status_code == 200
    assert "Waiver queue optimizer" in response.text
    assert 'data-queue-add="add"' in response.text
    assert "suggested across queue" in response.text


def test_insights_page_and_home_briefing_render_from_personalized_context(monkeypatch) -> None:
    web_app.sessions.clear()
    league = MFLLeague("11111", "0001", "Home League")
    current = web_app.BrowserSession("cookie", 2026, [league], "csrf")
    web_app.sessions["insights-session"] = current
    action = {"tone":"warning", "title":"Questionable starter", "detail":"Check Sunday status.",
              "href":"/lineup?league=11111", "label":"Review"}
    report = web_app.ProjectionAccuracyReport((), (), (), ())
    monkeypatch.setattr(web_app, "_load_insights", lambda *args, **kwargs: {
        "week":2, "rows":(), "actions":[action], "alert_count":1,
        "depth_updated":"", "accuracy":report, "reference_rows":(), "errors":{},
        "ranking_label":"Combined MFL + ESPN + ML position ranks",
        "projection_tracker":{"leader":{"source":"MFL", "mae":2.5}, "positions":(),
                              "espn_hit_rate":60.0, "espn_samples":10, "sources":()},
    })
    client = TestClient(web_app.app)
    client.cookies.set("wp_session", "insights-session")
    page = client.get("/insights?league=11111")
    assert page.status_code == 200
    assert "Roster intelligence" in page.text
    assert "Questionable starter" in page.text
    assert "Projection tracker" in page.text and "Misses by 2.50 points" in page.text
    briefing = client.get("/hub/briefing?league=11111")
    assert briefing.status_code == 200
    assert "personalized from your saved MFL lineup" in briefing.text


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
        espn_ranks={"a1": 8.5},
        espn_matched=1,
        espn_source="ESPN weekly consensus (PPR)",
        combined_ranks={"a1": 2.0},
        combined_matched=1,
        combined_source="Equal-weight MFL + ESPN + StatHead position ranks",
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
    assert "ESPN #8.5" in response.text
    assert "Combined 2.0 WR rank" in response.text
    assert '<option value="combined-rank"' in response.text
    assert '<option value="espn-rank" selected>ESPN weekly rank</option>' in response.text
    assert "Strong target" in response.text
    assert "Locked Prospect" in response.text
    assert "Waiver claim only" in response.text
    assert 'value="a2"' in response.text
    locked_control = response.text.split('value="a2"', 1)[1].split(">", 1)[0]
    assert "disabled" not in locked_control
    assert 'data-waiver-only="true"' in locked_control
    assert "YTD" in response.text
    assert "Avg" in response.text
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


@pytest.mark.parametrize("pricing_unavailable", [False, True])
def test_defense_streaming_cards_use_loaded_data_and_existing_move_builder(monkeypatch, pricing_unavailable) -> None:
    from types import SimpleNamespace
    from tests.test_defense_streaming import defense, game

    session_id = "defense-stream-session"
    web_app.sessions[session_id] = web_app.BrowserSession(
        mfl_cookie="synthetic", year=2026,
        leagues=[MFLLeague("11111", "0001", "Home League")], csrf_token="csrf",
    )
    board = [defense("HST"), defense("DET", owned=True), defense("SEA", locked=True)]
    fake = SimpleNamespace(week_games={"HOU": game("MIA", kickoff=4_000_000_000),
                                     "DET": game("BUF", kickoff=4_000_000_000),
                                     "SEA": game("ARI", kickoff=4_000_000_000)},
                           opponent_strength={})
    reads = {"details": 0, "activity": 0}

    def details():
        reads["details"] += 1
        return MFLLeagueDetails((), {"0001": MFLFranchise("0001", "My Team", faab_balance=43)})

    def transactions(**kwargs):
        assert kwargs == {"days": 21, "count": 200}
        reads["activity"] += 1
        if pricing_unavailable:
            raise MFLApiError("Synthetic unavailable activity")
        return (MFLTransaction("def-award", "BBID_WAIVER", int(web_app.time.time()) - 100,
                               ("0002",), ("HST",), (), bid=3),)

    fake.league_details = details
    fake.transactions = transactions
    web_app.sessions[session_id].player_catalog = {item.player.id: item.player for item in board}
    monkeypatch.setattr(web_app, "_client", lambda *args: fake)
    monkeypatch.setattr(web_app, "_remember_catalog", lambda *args: None)
    monkeypatch.setattr(web_app, "_load_player_board", lambda *args: (
        2, [board[1].player], board, ProjectionBlend(scores={}, mfl_scores={}, ml_scores={}, ml_matched=0), set()))
    client = TestClient(web_app.app)
    client.cookies.set("wp_session", session_id)
    response = client.get("/moves?league=11111")
    assert response.status_code == 200
    panel = response.text.split('<details class="defense-streaming"', 1)[1].split('<form method="post"', 1)[0]
    assert "Your defense" in panel and "Available streaming targets" in panel
    assert 'data-stream-pick="HST"' in panel
    assert 'data-stream-pick="DET"' not in panel
    assert 'data-stream-pick="SEA"' in panel and "Select waiver target" in panel
    assert "98.4" in panel and "8.0 pts" in panel
    assert "2026-09-09" in panel and "Not live offensive performance" in panel
    assert "43 FAAB remaining" in panel and "Suggested bid:" in panel
    assert 'data-stream-bid=' in panel
    if pricing_unavailable:
        assert "budget-only heuristic" in panel
        assert "No verified recent defense prices" in panel
    else:
        assert "3 FAAB" in panel
        assert client.get("/moves?league=11111").status_code == 200
        assert reads == {"details": 1, "activity": 1}
        assert f"2026:11111:report:activity" in web_app.sessions[session_id].read_cache
    assert "20260919-waiver-optimizer" in response.text
    assert "themes.css" in response.text and "viewport-fit=cover" in response.text


def test_player_market_keeps_team_defense_and_removes_idp() -> None:
    assert web_app._include_on_player_board(MFLPlayer("def", "Lions Defense", "Def", "DET"))
    assert web_app._board_position(MFLPlayer("def", "Lions Defense", "Def", "DET")) == "DEF"
    for position in ("DE", "DT", "LB", "CB", "S", "DB", "DL", "EDGE"):
        assert not web_app._include_on_player_board(MFLPlayer(position, "IDP", position, "DET"))
    assert web_app._include_on_player_board(MFLPlayer("wr", "Receiver", "WR", "DET"))


def test_rosters_tab_shows_every_member_and_groups_roster_tools(monkeypatch) -> None:
    web_app.sessions.clear()
    session_id = "league-rosters-session"
    league = MFLLeague("77777", "0001", "Roster League")
    current = web_app.BrowserSession("unique-roster-cookie", 2026, [league], "csrf")
    web_app.sessions[session_id] = current
    reads = {"details": 0, "rosters": 0, "players": 0}

    class RosterClient:
        def league_details(self):
            reads["details"] += 1
            return MFLLeagueDetails((), {
                "0001": MFLFranchise("0001", "My Franchise"),
                "0002": MFLFranchise("0002", "Division Rival"),
            }, name="Roster League")

        def trade_rosters(self):
            reads["rosters"] += 1
            return {"0001": {"mine"}, "0002": {"qb", "def"}}

        def players(self):
            reads["players"] += 1
            return {
                "mine": MFLPlayer("mine", "My Runner", "RB", "DET"),
                "qb": MFLPlayer("qb", "Rival Quarterback", "QB", "BUF"),
                "def": MFLPlayer("def", "Lions Defense", "Def", "DET"),
            }

    monkeypatch.setattr(web_app, "_client", lambda *args: RosterClient())
    client = TestClient(web_app.app)
    client.cookies.set("wp_session", session_id)

    response = client.get("/rosters?league=77777")

    assert response.status_code == 200
    assert "League rosters" in response.text
    assert "My Franchise" in response.text and "Division Rival" in response.text
    assert "Rival Quarterback" in response.text and "Lions Defense" in response.text
    assert 'href="/trades?league=77777&target=0002"' in response.text
    assert response.text.index("My Franchise") < response.text.index("Division Rival")
    assert "Free agents" in response.text and "Add, drop &amp; waivers" in response.text
    top_nav = response.text.split('<nav class="section-nav"', 1)[1].split("</nav>", 1)[0]
    assert "Rosters" in top_nav and ">Players<" not in top_nav and ">Trades<" not in top_nav

    assert client.get("/rosters?league=77777").status_code == 200
    assert reads == {"details": 1, "rosters": 1, "players": 1}


def test_player_leaders_show_official_ranks_ownership_and_primary_rank(monkeypatch) -> None:
    web_app.sessions.clear()
    session_id = "league-leaders-session"
    league = MFLLeague("77778", "0001", "Leader League")
    current = web_app.BrowserSession("leader-cookie", 2026, [league], "csrf")
    current.ranking_preference = "combined"
    web_app.sessions[session_id] = current
    players = {
        "qb": MFLPlayer("qb", "Alpha Quarterback", "QB", "DET"),
        "rb": MFLPlayer("rb", "Beta Runner", "RB", "GB"),
        "fa": MFLPlayer("fa", "Gamma Receiver", "WR", "BUF"),
        "def": MFLPlayer("def", "Detroit Defense", "Def", "DET"),
        "idp": MFLPlayer("idp", "Hidden Linebacker", "LB", "MIN"),
    }

    class LeaderClient:
        config = MFLConfig(2026, league.id, league.franchise_id, user_cookie="cookie")
        session = None
        def players(self): return players
        def trade_rosters(self): return {"0001": {"qb"}, "0002": {"rb"}}
        def league_details(self):
            return MFLLeagueDetails((), {
                "0001": MFLFranchise("0001", "My Team"),
                "0002": MFLFranchise("0002", "Rival Team"),
            })
        def player_scores(self, period, **kwargs):
            return {"qb": 70.0, "rb": 55.0, "fa": 40.0, "def": 32.0, "idp": 100.0} if period == "YTD" else {
                "qb": 23.3, "rb": 18.3, "fa": 13.3, "def": 10.7, "idp": 33.3,
            }
        def current_week(self): return 4
        def projected_scores(self, **kwargs): return {"qb": 24.0, "rb": 17.0, "fa": 15.0, "def": 8.0}

    monkeypatch.setattr(web_app, "_client", lambda *args: LeaderClient())
    monkeypatch.setattr(
        web_app,
        "_load_reference_projection_blend",
        lambda *args, **kwargs: ProjectionBlend(
            kwargs["mfl_scores"], kwargs["mfl_scores"], {}, 0,
            combined_ranks={"qb": 1.0, "rb": 2.0, "fa": 3.0, "def": 1.0}, combined_matched=4,
        ),
    )
    client = TestClient(web_app.app)
    client.cookies.set("wp_session", session_id)

    response = client.get("/leaders?league=77778")

    assert response.status_code == 200
    assert "League leaders" in response.text and "YTD player rankings" in response.text
    assert "Alpha Quarterback" in response.text and "#1" in response.text
    assert "My Team" in response.text and "Rival Team" in response.text and "Free agent" in response.text
    assert "Hidden Linebacker" not in response.text
    assert "Combined MFL + ESPN + ML position rank" in response.text
    tabs = response.text.split('<nav class="roster-tools"', 1)[1].split("</nav>", 1)[0]
    assert tabs.index("Compare") < tabs.index("League leaders")
    position_select = response.text.split('<select name="position">', 1)[1].split("</select>", 1)[0]
    assert position_select.index("WR + TE") < position_select.index("FLEX (RB + WR + TE)")
    assert position_select.rfind(">DEF<") > position_select.index("FLEX (RB + WR + TE)")

    wr_te = client.get("/leaders", params={"league": "77778", "position": "WR+TE"})
    assert "Gamma Receiver" in wr_te.text and "Beta Runner" not in wr_te.text
    flex = client.get("/leaders", params={"league": "77778", "position": "FLEX"})
    assert "Gamma Receiver" in flex.text and "Beta Runner" in flex.text
    assert "Alpha Quarterback" not in flex.text


def test_watchlist_toggle_and_player_compare_use_league_scored_board(monkeypatch) -> None:
    web_app.sessions.clear()
    league = MFLLeague("88881", "0001", "Tools League")
    current = web_app.BrowserSession("tools-watch-cookie", 2026, [league], "csrf")
    web_app.sessions["tools-watch"] = current
    first = PlayerRecommendation(
        MFLPlayer("101", "Alpha Runner", "RB", "DET"), MFLAvailability("101"),
        15.5, 2.0, None, "Upgrade", "good", "Projects as a weekly starter.",
    )
    second = PlayerRecommendation(
        MFLPlayer("102", "Beta Runner", "RB", "GB"), MFLAvailability("102"),
        12.0, 0.5, None, "Small edge", "fair", "Useful depth.",
    )

    class ToolsClient:
        def players(self): return {"101": first.player, "102": second.player}
        def injuries(self, **kwargs): return {}

    fake = ToolsClient()

    def board(client, *args, **kwargs):
        client.player_ytd_scores = {"101": 31.0, "102": 22.0}
        client.player_avg_scores = {"101": 15.5, "102": 11.0}
        client.player_median_scores = {"101": 15.0, "102": 10.0}
        client.opponent_strength = {"101": {"opponent": "MIN", "position": "RB", "rank": 4, "label": "Favorable"}}
        return 2, [], [first, second], ProjectionBlend({}, {}, {}, 0), set()

    monkeypatch.setattr(web_app, "_client", lambda *args: fake)
    monkeypatch.setattr(web_app, "_load_player_board", board)
    client = TestClient(web_app.app)
    client.cookies.set("wp_session", "tools-watch")

    added = client.post("/api/watchlist/101?league=88881", headers={"X-CSRF-Token": "csrf"})
    assert added.status_code == 200 and added.json() == {"watched": True, "count": 1}
    watched = client.get("/watchlist?league=88881")
    assert watched.status_code == 200 and "Alpha Runner" in watched.text and "31.0" in watched.text
    compared = client.get("/compare?league=88881&p1=101&p2=102")
    assert compared.status_code == 200
    assert "Alpha Runner" in compared.text and "Beta Runner" in compared.text
    assert "No MFL designation" in compared.text and "MIN · #4 Favorable" in compared.text


def test_database_status_reports_local_storage_without_database_url(monkeypatch) -> None:
    monkeypatch.delenv("WP_DATABASE_URL", raising=False)

    assert web_app._database_status() == {
        "state": "local",
        "label": "Local storage",
        "backend": "SQLite",
        "schema_version": None,
        "detail": (
            "Supabase is not configured on this deployment. "
            "Local encrypted session storage remains active."
        ),
    }


def test_database_status_hides_connection_error_details(monkeypatch) -> None:
    class UnavailableStore:
        def connection_status(self):
            raise RuntimeError("postgresql://user:secret@example.test/postgres")

    events = []
    monkeypatch.setenv("WP_DATABASE_URL", "postgresql://configured")
    monkeypatch.setattr(web_app, "_persistent_store", lambda: UnavailableStore())
    monkeypatch.setattr(web_app, "log_error", lambda event, error: events.append(event))

    status = web_app._database_status()

    assert status["state"] == "unavailable"
    assert status["label"] == "Database unavailable"
    assert "secret" not in repr(status)
    assert events == ["database_status_unavailable"]


def test_operations_schedule_rules_status_and_guide_pages_render(monkeypatch) -> None:
    web_app.sessions.clear()
    league = MFLLeague("88882", "0001", "Operations League")
    current = web_app.BrowserSession("tools-pages-cookie", 2026, [league], "csrf")
    current.operations.append(web_app.OperationRecord(
        "88882", "waiver", "Add Player A · drop Player B", "completed", "MFL accepted the request.",
    ))
    web_app.sessions["tools-pages"] = current

    class ToolsClient:
        def transactions(self, **kwargs):
            return (MFLTransaction("tx", "FREE_AGENT", 1_700_000_000, ("0001",), ("101",), ("102",)),)
        def players(self):
            return {"101": MFLPlayer("101", "Player A", "RB", "DET"), "102": MFLPlayer("102", "Player B", "RB", "GB")}
        def league_details(self):
            return MFLLeagueDetails((), {
                "0001": MFLFranchise("0001", "My Team"), "0002": MFLFranchise("0002", "Opponent"),
            }, name="Operations League", start_week=1, end_week=18, last_regular_season_week=14, faab_limit=100)
        def fantasy_schedule(self):
            return (MFLFantasyGame(2, ("0001", "0002"), (101.5, 99.0)),)
        def current_week(self): return 2
        def lineup_settings(self):
            return MFLLineupSettings(2, (MFLLineupRule("QB", 1, 1), MFLLineupRule("RB|WR", 1, 1)))
        def scoring_rules(self):
            return {"positionRules": {"positions": "RB|WR", "rule": {"event": "RY", "range": "0-999", "points": "*.1"}}}

    monkeypatch.setattr(web_app, "_client", lambda *args: ToolsClient())
    monkeypatch.setattr(web_app, "_database_status", lambda: {
        "state": "connected", "label": "Supabase connected",
        "backend": "Supabase PostgreSQL", "schema_version": 1,
        "detail": "This server reached the private fantasy_hq schema successfully.",
    })
    client = TestClient(web_app.app)
    client.cookies.set("wp_session", "tools-pages")

    transactions = client.get("/transactions?league=88882")
    assert transactions.status_code == 200
    assert "My transactions" in transactions.text and "Player A" in transactions.text
    schedule = client.get("/schedule?league=88882")
    assert schedule.status_code == 200 and "101.50" in schedule.text and "Opponent" in schedule.text
    rules = client.get("/rules?league=88882")
    assert rules.status_code == 200 and "RB|WR" in rules.text and "*.1" in rules.text
    data_status = client.get("/data-status?league=88882")
    assert data_status.status_code == 200
    assert "Supabase connected" in data_status.text
    assert "fantasy_hq · migration 1" in data_status.text
    assert "Connection details, passwords, tokens" in data_status.text
    guide = client.get("/guide?league=88882")
    assert guide.status_code == 200 and "Four moves to get set" in guide.text


def test_notification_center_and_first_run_guide(monkeypatch) -> None:
    web_app.sessions.clear()
    league = MFLLeague("88883", "0001", "Alert League")
    current = web_app.BrowserSession("tools-alert-cookie", 2026, [league], "csrf")
    web_app.sessions["tools-alert"] = current
    monkeypatch.setattr(web_app, "_load_insights", lambda *args, **kwargs: {
        "actions": [{"tone": "warning", "title": "Questionable starter", "detail": "Check Sunday status.",
                     "href": "/lineup?league=88883", "label": "Review"}],
    })
    class AlertClient:
        def transactions(self, **kwargs): return ()
    monkeypatch.setattr(web_app, "_client", lambda *args: AlertClient())
    client = TestClient(web_app.app)
    client.cookies.set("wp_session", "tools-alert")
    alerts = client.get("/notifications?league=88883")
    assert alerts.status_code == 200 and "Questionable starter" in alerts.text
    home = client.get("/home?league=88883")
    assert "Which rankings should lead your player lists?" in home.text and "My transactions" in home.text
    saved = client.post("/preferences/rankings", data={
        "league": "88883", "ranking_preference": "espn-ppr", "csrf_token": "csrf",
    }, follow_redirects=False)
    assert saved.status_code == 303 and saved.cookies.get("wp_rankings") == "espn-ppr"
    assert current.ranking_preference == "espn-ppr"
    home = client.get("/home?league=88883")
    assert "Your league is connected" in home.text
    dismissed = client.post("/onboarding/dismiss", data={"league": "88883", "csrf_token": "csrf"}, follow_redirects=False)
    assert dismissed.status_code == 303 and dismissed.cookies.get("wp_tour_done") == "1"


def test_ranking_preference_is_csrf_protected_and_changeable(monkeypatch) -> None:
    web_app.sessions.clear()
    league = MFLLeague("88884", "0001", "Rank League")
    current = web_app.BrowserSession("rank-cookie", 2026, [league], "csrf")
    web_app.sessions["rank-session"] = current
    client = TestClient(web_app.app)
    client.cookies.set("wp_session", "rank-session")

    assert client.post("/preferences/rankings", data={
        "league": league.id, "ranking_preference": "mfl", "csrf_token": "wrong",
    }).status_code == 403
    invalid = client.post("/preferences/rankings", data={
        "league": league.id, "ranking_preference": "unknown", "csrf_token": "csrf",
    })
    assert invalid.status_code == 400
    saved = client.post("/preferences/rankings", data={
        "league": league.id, "ranking_preference": "mfl", "csrf_token": "csrf",
    }, follow_redirects=False)
    assert saved.status_code == 303 and saved.cookies.get("wp_rankings") == "mfl"
    assert current.ranking_preference == "mfl"
    combined = client.post("/preferences/rankings", data={
        "league": league.id, "ranking_preference": "combined", "csrf_token": "csrf",
    }, follow_redirects=False)
    assert combined.status_code == 303 and combined.cookies.get("wp_rankings") == "combined"
    assert current.ranking_preference == "combined"


def test_player_card_returns_bounded_weekly_totals_and_trend(monkeypatch) -> None:
    web_app.sessions.clear()
    league = MFLLeague("88885", "0001", "Trend League")
    current = web_app.BrowserSession("trend-cookie", 2026, [league], "csrf")
    web_app.sessions["trend-session"] = current
    player = MFLPlayer("p1", "Trend Runner", "RB", "DET")
    weekly = {1: 5.0, 2: 10.0, 3: 15.0, 4: 20.0}

    class CardClient:
        config = MFLConfig(2026, league.id, league.franchise_id, user_cookie="cookie")
        def __init__(self): self._players = {player.id: player}
        def players(self): return self._players
        def projected_scores(self, **kwargs): return {player.id: 18.5}
        def live_scoring(self, **kwargs):
            live_player = MFLLivePlayer(player.id, 20.0, "starter", 0)
            team = MFLLiveFranchise("0001", 20.0, False, 0, 0, 0, (live_player,))
            return MFLLiveScoring(4, (MFLLiveMatchup((team,)),))
        def player_scores(self, period, **kwargs):
            if period == "YTD": return {player.id: 50.0}
            if period == "AVG": return {player.id: 12.5}
            return {player.id: weekly[int(period)]}
        def scoring_rules(self): return {}

    monkeypatch.setattr(web_app, "_client", lambda *args: CardClient())
    monkeypatch.setattr(
        web_app,
        "_load_reference_projection_blend",
        lambda *args, **kwargs: ProjectionBlend(
            {player.id: 18.5}, {player.id: 18.5}, {}, 0,
            espn_ranks={player.id: 4.0}, espn_matched=1,
            combined_ranks={player.id: 2.5}, combined_matched=1,
        ),
    )
    client = TestClient(web_app.app)
    client.cookies.set("wp_session", "trend-session")
    response = client.get("/api/players/p1?league=88885&week=4")
    assert response.status_code == 200
    data = response.json()
    assert [row["points"] for row in data["weekly_points"]] == [5.0, 10.0, 15.0, 20.0]
    assert data["season_summary"] == {
        "ytd": 50.0, "average": 12.5, "recent_average": 15.0, "high": 20.0, "games": 4,
    }
    assert data["trend"]["direction"] == "up" and data["trend"]["delta"] == 10.0
    assert data["ranking_preference"] == "ESPN PPR weekly consensus"
    assert data["league_rank"] == {"overall": 1, "position_rank": 1, "position": "RB"}
    assert data["primary_rank"] == {
        "rank": 4.0, "position": "RB", "label": "ESPN PPR weekly consensus",
    }


def test_projection_tracker_summary_identifies_leaders_and_weighted_rank_signal() -> None:
    report = ProjectionAccuracyReport(
        (1, 2),
        (
            AccuracyMetric("MFL", "QB", 10, 3.0, 4.0, 1.0),
            AccuracyMetric("StatHead ML · scaled", "QB", 10, 4.0, 5.0, -1.0),
            AccuracyMetric("MFL", "WR", 20, 5.0, 6.0, 0.5),
            AccuracyMetric("StatHead ML · scaled", "WR", 20, 4.0, 5.0, -0.5),
        ),
        (RankMetric("QB", 10, 60.0), RankMetric("WR", 20, 75.0)),
        (),
    )
    tracker = web_app._projection_tracker_summary(report)
    assert tracker["leader"]["source"] == "StatHead ML · scaled"
    assert tracker["leader"]["mae"] == 4.0
    assert tracker["espn_hit_rate"] == 70.0
    assert {row["position"]: row["leader"] for row in tracker["positions"]} == {
        "QB": "MFL", "WR": "StatHead ML · scaled",
    }


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
    assert "20260915-matchup-context" in response.text


def test_incomplete_lineup_preview_url_redirects_safely() -> None:
    web_app.sessions.clear()
    client = TestClient(web_app.app)
    assert client.get("/lineup/preview", follow_redirects=False).headers["location"] == "/"
    web_app.sessions["preview-session"] = web_app.BrowserSession(
        "cookie", 2026, [MFLLeague("11111", "0001", "League")], "csrf",
    )
    client.cookies.set("wp_session", "preview-session")
    response = client.get("/lineup/preview", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"


def test_lineup_global_reads_are_shared_across_leagues(monkeypatch) -> None:
    current = web_app.BrowserSession(
        "cookie", 2026,
        [MFLLeague("11111", "0001", "One"), MFLLeague("22222", "0002", "Two")],
        "csrf",
    )
    reads = {"week": 0, "schedule": 0, "injuries": 0, "live": 0, "roster": 0, "status": 0}

    class LineupClient:
        def __init__(self, league_id, franchise_id):
            self.config = MFLConfig(2026, league_id, franchise_id, user_cookie="cookie")
            self.session = None
            self.lineup_visible = True
            self.lineup_scores = {}

        def current_week(self): reads["week"] += 1; return 2
        def roster_ids(self): reads["roster"] += 1; return {f"p-{self.config.league_id}"}
        def named_players(self, ids): return [MFLPlayer(next(iter(ids)), "Player", "WR", "DET")]
        def lineup_settings(self): return MFLLineupSettings(1, (MFLLineupRule("WR", 1, 1),))
        def player_roster_statuses(self, ids, *, week): reads["status"] += 1; return {next(iter(ids)): "S"}
        def nfl_team_kickoffs(self, *, week):
            reads["schedule"] += 1
            self.week_games = {"DET": {"opponent": "vs BUF", "kickoff": 9999999999}}
            return {"DET": 9999999999}
        def live_scoring(self, *, week):
            reads["live"] += 1
            player_id = f"p-{self.config.league_id}"
            return MFLLiveScoring(week, (MFLLiveMatchup((MFLLiveFranchise(
                self.config.franchise_id, 0, True, 1, 0, 3600,
                (MFLLivePlayer(player_id, 0, "starter", 3600),),
            ),)),))
        def projected_scores(self, **kwargs): return {}
        def injuries(self, **kwargs): reads["injuries"] += 1; return {}

    monkeypatch.setattr(
        web_app, "projection_blend",
        lambda players, **kwargs: ProjectionBlend({}, {}, {}, 0),
    )
    first = LineupClient("11111", "0001")
    second = LineupClient("22222", "0002")
    web_app._load_lineup(first, current=current)
    web_app._load_lineup(second, current=current)
    assert reads == {
        "week": 1, "schedule": 1, "injuries": 1,
        "live": 2, "roster": 0, "status": 0,
    }
    assert second.week_games == first.week_games


def test_client_uses_the_league_specific_mfl_host() -> None:
    league = MFLLeague(
        "11111", "0001", "Home League",
        "https://www42.myfantasyleague.com/2026/home/11111",
    )
    current = web_app.BrowserSession("cookie", 2026, [league], "csrf")
    assert web_app._client(current, league).config.base_url == "https://www42.myfantasyleague.com"


def test_uncertain_lineup_write_is_verified_by_readback(monkeypatch) -> None:
    web_app.sessions.clear()
    session_id = "lineup-submit-session"
    league = MFLLeague("11111", "0001", "Home League")
    starter = MFLPlayer("p1", "Starter", "WR", "DET")
    bench = MFLPlayer("p2", "Bench", "WR", "GB")
    preview = web_app.LineupPreview(
        league_id=league.id,
        franchise_id=league.franchise_id,
        week=1,
        current_starters=(bench,),
        starters=(starter,),
        current_projection=8.0,
        projected_total=12.0,
    )
    current = web_app.BrowserSession("cookie", 2026, [league], "csrf")
    current.pending_lineups["pending"] = preview
    live_cache_key = "2026:11111:report:live-scoring:1"
    current.read_cache[live_cache_key] = (9999999999, "old lineup")
    web_app.sessions[session_id] = current

    class UncertainLineupClient:
        def __init__(self):
            self.status_reads = 0
            self.submit_calls = 0

        def roster_ids(self): return {"p1", "p2"}
        def named_players(self, ids): return [player for player in (starter, bench) if player.id in ids]
        def player_roster_statuses(self, ids, *, week):
            self.status_reads += 1
            return {"p1": "NS", "p2": "S"} if self.status_reads == 1 else {"p1": "S", "p2": "NS"}
        def lineup_settings(self): return MFLLineupSettings(1, (MFLLineupRule("WR", 1, 1),))
        def nfl_team_kickoffs(self, *, week): return {}
        def submit_lineup(self, **kwargs):
            self.submit_calls += 1
            raise MFLWriteUncertainError("unreadable receipt")

    fake = UncertainLineupClient()
    monkeypatch.setattr(web_app, "_client", lambda current, selected: fake)
    client = TestClient(web_app.app)
    client.cookies.set("wp_session", session_id)
    response = client.post(
        "/lineup/submit/pending",
        data={"csrf_token": "csrf"},
    )
    assert response.status_code == 200
    assert "starters were verified after submission" in response.text
    assert fake.status_reads == 2
    assert fake.submit_calls == 1
    assert live_cache_key not in current.read_cache


def test_lineup_submit_get_redirects_without_mutating_state() -> None:
    web_app.sessions.clear()
    session_id = "lineup-submit-get-session"
    league = MFLLeague("11111", "0001", "Home League")
    player = MFLPlayer("p1", "Starter", "WR", "DET")
    current = web_app.BrowserSession("cookie", 2026, [league], "csrf")
    current.pending_lineups["pending"] = web_app.LineupPreview(
        league.id, league.franchise_id, 1, (), (player,), 0, 10,
    )
    web_app.sessions[session_id] = current
    client = TestClient(web_app.app)
    client.cookies.set("wp_session", session_id)
    response = client.get("/lineup/submit/pending", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/lineup/preview/pending"
    assert "pending" in current.pending_lineups
    expired = client.get("/lineup/submit/expired", follow_redirects=False)
    assert expired.status_code == 303
    assert expired.headers["location"] == "/dashboard"


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
    bracket_item = {
        "game": MFLFantasyGame(15, ("0001", "0002"), (None, None)),
        "probability": (60, 40), "seeds": (1, 8),
    }
    hq = {
        "details": MFLLeagueDetails((), teams, name="Test HQ", end_week=17),
        "teams": teams, "names": names, "current_week": 2,
        "standings": [{"id":"0001","h2hw":"1","h2hl":"0","h2ht":"0","pf":"120","pa":"100"}],
        "groups": [{"id":"","name":"League standings","rows":[{"id":"0001","h2hw":"1","h2hl":"0","h2ht":"0","pf":"120","pa":"100"}]}],
        "schedule": games, "activity": activity,
        "message_threads": (MFLMessageThread("thread-1", "Trash talk", "0002", timestamp=100, replies=2),),
        "chat_messages": (MFLChatMessage("chat-1", "Good luck", "0002", 100),),
        "last_results_week": 1,
        "last_week_results": [{"week": 1, "teams": (
            {"id": "0001", "name": "Alpha", "logo_url": "", "score": 120.0, "winner": True},
            {"id": "0002", "name": "Bravo", "logo_url": "", "score": 100.0, "winner": False},
        )}],
        "catalog": {"p1": MFLPlayer("p1", "Pickup", "WR", "DET"), "p2": MFLPlayer("p2", "Drop", "RB", "GB")},
        "rankings": rankings, "rank_by_team": {row.franchise_id: row for row in rankings},
        "recap": build_recap(games, names, current_week=2), "trends": waiver_trends(activity),
        "playoff_games": [bracket_item],
        "playoff_rounds": [
            {"name": "Quarterfinals", "games": [bracket_item]},
            {"name": "Semifinals", "games": [{**bracket_item, "projected_advancement": True}]},
            {"name": "Championship", "games": [{**bracket_item, "projected_advancement": True}]},
        ],
        "errors": {},
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
    assert "/static/league.css?v=20260920-social-bottom" in response.text
    assert "/static/interface.js?v=4" in response.text
    assert 'class="bracket-round bracket-round-3"' in response.text
    assert "Championship" in response.text
    assert "Projected advancement" in response.text
    assert "Week 1 results" in response.text
    assert "120.00" in response.text
    assert "Side-bet tracker" in response.text
    assert "Message board" in response.text and "League chat" in response.text
    assert response.text.index('id="league-social"') > response.text.index('id="side-bets"')
    assert response.text.index('id="league-chat"') > response.text.index('id="side-bets"')
    assert "Trash talk" in response.text and "Good luck" in response.text
    assert "/league/message-thread/thread-1?league=11111" in response.text
    assert 'action="/league/social/preview"' in response.text
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


def test_social_posts_require_review_csrf_and_only_send_once(monkeypatch) -> None:
    web_app.sessions.clear()
    league = MFLLeague("11111", "0001", "Home League")
    current = web_app.BrowserSession("cookie", 2026, [league], "csrf")
    current.read_cache["2026:11111:report:message-board"] = (9999999999, "old")
    web_app.sessions["social"] = current
    writes = []

    class SocialClient:
        def post_message_board(self, **kwargs): writes.append(("board", kwargs))
        def post_chat(self, **kwargs): writes.append(("chat", kwargs))
        def league_details(self):
            return MFLLeagueDetails((), {"0001": MFLFranchise("0001", "Alpha"), "0002": MFLFranchise("0002", "Bravo")})

    monkeypatch.setattr(web_app, "_client", lambda *args: SocialClient())
    client = TestClient(web_app.app)
    client.cookies.set("wp_session", "social")
    preview = client.post("/league/social/preview", data={
        "league":"11111", "kind":"board-thread", "subject":"Week 3",
        "body":"<script>alert(1)</script>", "csrf_token":"csrf",
    }, follow_redirects=False)
    assert preview.status_code == 303 and not writes
    review_url = preview.headers["location"]
    review = client.get(review_url)
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in review.text
    assert client.get(review_url.replace("review", "send"), follow_redirects=False).status_code == 303
    assert not writes
    pending_id = review_url.rsplit("/", 1)[1]
    assert client.post(f"/league/social/send/{pending_id}", data={"csrf_token":"bad"}).status_code == 403
    sent = client.post(f"/league/social/send/{pending_id}", data={"csrf_token":"csrf"})
    assert "MFL accepted the message" in sent.text
    assert writes == [("board", {"subject":"Week 3", "body":"<script>alert(1)</script>", "thread_id":""})]
    assert client.post(f"/league/social/send/{pending_id}", data={"csrf_token":"csrf"}).status_code == 404
    assert len(writes) == 1
    assert not any("message-board" in key for key in current.read_cache)

    private = client.post("/league/social/preview", data={
        "league":"11111", "kind":"chat", "body":"private note",
        "to_franchise_id":"2", "csrf_token":"csrf",
    }, follow_redirects=False)
    chat_id = private.headers["location"].rsplit("/", 1)[1]
    client.post(f"/league/social/send/{chat_id}", data={"csrf_token":"csrf"})
    assert writes[-1] == ("chat", {"body":"private note", "to_franchise_id":"0002"})
    assert client.post("/league/social/preview", data={
        "league":"11111", "kind":"chat", "body":"x", "to_franchise_id":"9999", "csrf_token":"csrf",
    }).status_code == 400


def test_uncertain_chat_post_cannot_be_retried(monkeypatch) -> None:
    web_app.sessions.clear()
    league = MFLLeague("11111", "0001", "Home League")
    current = web_app.BrowserSession("cookie", 2026, [league], "csrf")
    web_app.sessions["social-uncertain"] = current
    attempts = []
    class Client:
        def post_chat(self, **kwargs):
            attempts.append(kwargs)
            raise MFLWriteUncertainError("Synthetic uncertain chat")
    monkeypatch.setattr(web_app, "_client", lambda *args: Client())
    client = TestClient(web_app.app); client.cookies.set("wp_session", "social-uncertain")
    preview = client.post("/league/social/preview", data={
        "league":"11111", "kind":"chat", "body":"one message", "csrf_token":"csrf",
    }, follow_redirects=False)
    pending_id = preview.headers["location"].rsplit("/", 1)[1]
    first = client.post(f"/league/social/send/{pending_id}", data={"csrf_token":"csrf"})
    assert "Do not submit again" in first.text
    assert client.post(f"/league/social/send/{pending_id}", data={"csrf_token":"csrf"}).status_code == 404
    assert attempts == [{"body":"one message", "to_franchise_id":""}]


def test_local_playoff_projection_builds_full_three_round_bracket(monkeypatch) -> None:
    league = MFLLeague("11111", "0001", "League")
    current = web_app.BrowserSession("cookie", 2026, [league], "csrf")
    teams = {
        f"{index:04d}": MFLFranchise(
            f"{index:04d}", f"Team {index}", f"0{((index - 1) % 3) + 1}",
        )
        for index in range(1, 9)
    }
    standings = [
        {"id": team_id, "h2hw": str(9 - index), "h2hl": str(index - 1),
         "h2ht": "0", "pf": str(1000 - index * 10), "pa": "800"}
        for index, team_id in enumerate(teams, 1)
    ]

    class BracketClient:
        config = MFLConfig(2026, league.id, league.franchise_id, user_cookie="cookie")
        _players = {}
        def league_details(self):
            return MFLLeagueDetails(
                (("01", "One"), ("02", "Two"), ("03", "Three")), teams,
                last_regular_season_week=14,
            )
        def current_week(self): return 2
        def league_standings(self): return standings
        def fantasy_schedule(self):
            return (MFLFantasyGame(1, tuple(teams), tuple(120 - index for index in range(8))),)
        def transactions(self, **kwargs): return ()
        def message_board(self, **kwargs): return ()
        def league_chat(self, **kwargs):
            return (
                MFLChatMessage("public", "League-wide", "0002", 3),
                MFLChatMessage("private-other", "Not for this owner", "0002", 2, "0003"),
                MFLChatMessage("private-own", "Sent by this owner", "0001", 1, "0003"),
            )
        def players(self): return {}

    monkeypatch.setattr(web_app, "_client", lambda *args: BracketClient())
    hq = web_app._league_hq(current, league)
    assert [len(round_.games) for round_ in hq["playoff_rounds"]] == [4, 2, 1]
    assert [round_.name for round_ in hq["playoff_rounds"]] == [
        "Quarterfinals", "Semifinals", "Championship",
    ]
    assert [item.id for item in hq["chat_messages"]] == ["public", "private-own"]


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


def test_opponent_strength_uses_position_specific_league_points_allowed() -> None:
    players = [
        MFLPlayer("qb", "Quarterback", "QB", "BUF"),
        MFLPlayer("wr", "Receiver", "WR", "BUF"),
    ]
    games = {"BUF": {"opponent_team": "DET", "opponent": "vs DET"}}
    allowed = {
        "DET": {"QB": 40.0, "WR+TE": 15.0},
        "KCC": {"QB": 20.0, "WR+TE": 30.0},
        "MIA": {"QB": 10.0, "WR+TE": 20.0},
    }
    strength = web_app._opponent_strength_by_player(players, games, allowed)
    assert strength["qb"]["rank"] == 1
    assert strength["qb"]["label"] == "Favorable"
    assert strength["wr"]["rank"] == 3
    assert strength["wr"]["label"] == "Tough"


def test_recent_player_median_is_bounded_to_five_completed_weeks() -> None:
    class ScoreClient:
        config = MFLConfig(2026, "11111", "0001")
        calls = []

        def player_scores(self, *, period):
            self.calls.append(period)
            if period == "YTD":
                return {"p1": 70.0}
            if period == "AVG":
                return {"p1": 14.0}
            return {"p1": float(period)}

    client = ScoreClient()
    ytd, average, median = web_app._load_player_score_summaries(client, None, 8)
    assert ytd == {"p1": 70.0}
    assert average == {"p1": 14.0}
    assert median == {"p1": 5.0}
    assert client.calls == ["YTD", "AVG", 3, 4, 5, 6, 7]
    assert client.player_median_window == 5


def test_game_locked_free_agent_can_be_staged_as_waiver_but_not_fcfs(monkeypatch) -> None:
    league = MFLLeague("11111", "0001", "League")
    current = web_app.BrowserSession("cookie", 2026, [league], "csrf")
    add = MFLPlayer("add", "Waiver Target", "WR", "BUF")
    drop = MFLPlayer("drop", "Drop Player", "WR", "DET")

    class MoveClient:
        _players = None
        def preview_add_drop(self, **kwargs):
            return AddDropPreview(
                kwargs["mode"], add, drop, league.id, league.franchise_id,
                bid=kwargs["bid"], round=kwargs["round_number"],
                replace_existing=kwargs["replace_existing"],
            )
        def current_week(self): return 2
        def nfl_team_kickoffs(self, *, week): return {"BUF": 1, "DET": 9999999999}

    monkeypatch.setattr(web_app, "_client", lambda *args: MoveClient())
    pending_id, preview, _ = web_app._stage_move(
        current, league_id=league.id, add_id=add.id, drop_id=drop.id,
        mode="waiver", bid=None, round_number=1,
    )
    assert pending_id in current.pending_moves
    assert preview.mode == "waiver"

    with pytest.raises(ValueError, match="Game already started"):
        web_app._stage_move(
            current, league_id=league.id, add_id=add.id, drop_id=drop.id,
            mode="fcfs", bid=None, round_number=None,
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
    assert "% estimated win" in response.text
    estimates = [part.rsplit(">", 1)[-1] for part in response.text.split("% estimated win")[:-1]]
    assert estimates and all("." in value and len(value.rsplit(".", 1)[-1]) == 2 for value in estimates)
    assert 'class="matchup-player-row"' in response.text
    # An explicit choice survives a return without a week query parameter.
    monkeypatch.setattr(web_app, "_load_live_scoring_week", lambda client, requested_week=None, **kwargs: (requested_week, 2, live, {}, head_to_head))
    returned = client.get("/scores?league=11111")
    assert "Week 1 Scores" in returned.text
