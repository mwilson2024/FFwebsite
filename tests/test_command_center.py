from fastapi.testclient import TestClient

from weekly_projections.lineup import LineupPlayerRecommendation, LineupRecommendation
from weekly_projections.mfl.client import (
    MFLLeague,
    MFLLineupRule,
    MFLLineupSettings,
    MFLPendingTrade,
    MFLPendingWaiver,
    MFLPlayer,
)
from weekly_projections.web import app as web


class CommandClient:
    def league_standings(self):
        return [
            {"id": "0001", "h2hw": "2", "h2hl": "1", "h2ht": "0", "pf": "321.4", "pa": "290.1", "vp": "0"},
            {"id": "0002", "h2hw": "1", "h2hl": "2", "h2ht": "0", "pf": "290.1", "pa": "321.4", "vp": "0"},
        ]

    def pending_waivers(self):
        return (MFLPendingWaiver("w1", ("3",), ("4",), 1, 1, 7),)

    def pending_trades(self):
        return (
            MFLPendingTrade("t1", "0002", "0001", ("1",), ("2",)),
            MFLPendingTrade("t2", "0001", "0002", ("2",), ("1",)),
        )


def _lineup():
    starter = MFLPlayer("1", "Starter", "QB", "DET")
    bench = MFLPlayer("2", "Bench", "QB", "BUF")
    rows = (
        LineupPlayerRecommendation(starter, 20.0, None, "starter", True, True, False, "KEEP", "good"),
        LineupPlayerRecommendation(bench, 25.0, None, "nonstarter", False, True, False, "START", "strong"),
    )
    return LineupRecommendation(rows, frozenset({"1"}), frozenset({"1", "2"}), 20.0, 45.0, 25.0, True)


def _session(monkeypatch):
    league = MFLLeague("league", "0001", "Test League")
    current = web.BrowserSession("cookie", 2026, [league], "csrf")
    current.watchlists[league.id] = {"1", "2"}
    monkeypatch.setattr(web, "sessions", {"command": current})
    monkeypatch.setattr(web, "_client", lambda *args: CommandClient())
    lineup = _lineup()
    monkeypatch.setattr(web, "_load_insights", lambda *args, **kwargs: {
        "week": 3,
        "lineup": lineup,
        "settings": MFLLineupSettings(2, (MFLLineupRule("QB", 1, 2),)),
        "actions": ({
            "tone": "danger", "title": "Fill one starter slot", "detail": "Saved lineup is incomplete.",
            "href": "/lineup?league=league&week=3", "label": "Fix lineup",
        },),
    })
    own_player = web.LivePlayerView(MFLPlayer("1", "Starter", "QB", "DET"), 12.0, "starter", 1800, 20.0)
    other_player = web.LivePlayerView(MFLPlayer("9", "Opponent", "QB", "GB"), 9.0, "starter", 1800, 18.0)
    matchup = web.HeadToHeadView((
        web.LiveTeamView("0001", "Mine", 12.0, True, 0, 1, (own_player,)),
        web.LiveTeamView("0002", "Theirs", 9.0, False, 0, 1, (other_player,)),
    ), 3, 3, MFLLineupSettings(1, (MFLLineupRule("QB", 1, 1),)))
    monkeypatch.setattr(web, "_load_live_scoring_week", lambda *args, **kwargs: (3, 3, None, {}, matchup))
    client = TestClient(web.app)
    client.cookies.set("wp_session", "command")
    return client


def test_command_center_core_is_an_actionable_cross_league_brief(monkeypatch):
    data = _session(monkeypatch).get("/api/command-center/league").json()
    assert data["priority"] == "Needs action"
    assert data["game_state"] == "Live"
    assert data["lineup"]["open_slots"] == 1
    assert data["lineup"]["projected_gain"] == 25.0
    assert data["standing"] == {
        "rank": 1, "teams": 2, "record": "2-1", "points_for": "321.4", "unavailable": False,
    }
    assert data["projected_score"] == 22.0
    assert data["opponent_projected_score"] == 18.0
    assert data["watchlist_count"] == 2
    assert data["links"]["transactions"].startswith("/transactions?")
    assert "view=pending" in data["links"]["transactions"]


def test_command_center_queue_loads_after_core_and_splits_trade_direction(monkeypatch):
    client = _session(monkeypatch)
    data = client.get("/api/command-center/league/queue").json()
    assert data["waiver_claims"] == 1
    assert data["incoming_trades"] == 1
    assert data["outgoing_trades"] == 1
    assert data["pending_total"] == 3
    assert data["errors"] == []
