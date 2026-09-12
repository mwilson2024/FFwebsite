"""Isolated UI test server; synthetic data only, no MFL network or writes.

Run: python -m uvicorn ui_preview:app --app-dir tests --port 8766
"""
from types import SimpleNamespace

from weekly_projections.web import app as web
from weekly_projections.mfl.client import (
    MFLAvailability,
    MFLFranchise,
    MFLLeague,
    MFLLeagueDetails,
    MFLFantasyGame,
    MFLTransaction,
    MFLLineupRule,
    MFLLineupSettings,
    MFLLiveFranchise,
    MFLLiveMatchup,
    MFLLivePlayer,
    MFLLiveScoring,
    MFLPlayer,
)
from weekly_projections.projection_sources import ProjectionBlend

PLAYERS = [
    MFLPlayer("1", "Allen, Josh", "QB", "BUF", espn_id="3918298", jersey="17"),
    MFLPlayer("2", "Hurts, Jalen", "QB", "PHI", espn_id="4040715", jersey="1"),
    MFLPlayer("3", "Barkley, Saquon", "RB", "PHI", espn_id="3929630", jersey="26"),
    MFLPlayer("4", "Gibbs, Jahmyr", "RB", "DET", espn_id="4429795", jersey="0"),
    MFLPlayer("5", "Chase, Ja'Marr", "WR", "CIN", espn_id="4362628", jersey="1"),
    MFLPlayer("6", "Jefferson, Justin", "WR", "MIN", espn_id="4262921", jersey="18"),
    MFLPlayer("7", "McBride, Trey", "TE", "ARI", espn_id="4432665", jersey="85"),
    MFLPlayer("8", "Smith, DeVonta", "WR", "PHI", espn_id="4241478", jersey="6"),
    MFLPlayer("9", "Henry, Derrick", "RB", "BAL", espn_id="3043078", jersey="22"),
    MFLPlayer("10", "Jackson, Lamar", "QB", "BAL", espn_id="3916387", jersey="8"),
    MFLPlayer("11", "St. Brown, Amon-Ra", "WR", "DET", espn_id="4374302", jersey="14"),
    MFLPlayer("12", "LaPorta, Sam", "TE", "DET", espn_id="4430027", jersey="87"),
    MFLPlayer("13", "Lions, Detroit", "Def", "DET"),
    MFLPlayer("14", "Packers, Green Bay", "Def", "GBP"),
    MFLPlayer("15", "Williams, Jameson", "WR", "DET", espn_id="4426388", jersey="9"),
    MFLPlayer("16", "Montgomery, David", "RB", "DET", espn_id="4035538", jersey="5"),
    MFLPlayer("17", "Kittle, George", "TE", "SFO", espn_id="3040151", jersey="85"),
    MFLPlayer("18", "Parsons, Micah", "LB", "DAL", espn_id="4361423", jersey="11"),
]
SCORES = dict(zip([p.id for p in PLAYERS], [24, 22, 21, 19, 22, 20, 15, 13, 16, 23, 18, 14, 8, 7, 14, 13, 12, 9]))
OWN_ROSTER = {"1", "3", "5", "7", "8", "9", "13"}
OTHER_ROSTER = {"2", "4", "6", "10", "11", "12", "16"}
OWN_STARTERS = {"1", "3", "5", "7", "8"}
OTHER_STARTERS = {"2", "4", "6", "11", "12"}
STATUSES = {player_id: ("S" if player_id in OWN_STARTERS else "NS") for player_id in OWN_ROSTER}


class DemoClient:
    config = SimpleNamespace(year=2026, franchise_id="0001", league_id="demo")
    session = None
    _players = {p.id: p for p in PLAYERS}
    def current_week(self): return 1
    def roster_ids(self): return set(OWN_ROSTER)
    def players(self): return dict(self._players)
    def franchise_names(self): return {"0001":"Motor City Maulers", "0002":"Ann Arbor Aces"}
    def franchise_roster(self, fid): return set(OWN_ROSTER if str(fid).zfill(4) == "0001" else OTHER_ROSTER)
    def trade_rosters(self): return {"0001":set(OWN_ROSTER), "0002":set(OTHER_ROSTER)}
    def free_agents(self):
        return {
            "14": MFLAvailability("14", "available"),
            "15": MFLAvailability("15", "waiver"),
            "17": MFLAvailability("17", "locked", locked=True),
            "18": MFLAvailability("18", "available"),
        }
    def league_details(self):
        return MFLLeagueDetails(
            (("01", "Leaders"), ("02", "Legends")),
            {
                "0001": MFLFranchise("0001", "Motor City Maulers", "01", faab_balance=74, waiver_order=2),
                "0002": MFLFranchise("0002", "Ann Arbor Aces", "02", faab_balance=91, waiver_order=1),
            },
            name="Motor City League", end_week=17, last_regular_season_week=14,
            faab_limit=100, history_years=(2026, 2025, 2024),
        )
    def league_standings(self):
        return [
            {"id":"0001", "h2hw":"1", "h2hl":"0", "h2ht":"0", "pf":"124.7", "pa":"103.2", "vp":"2"},
            {"id":"0002", "h2hw":"0", "h2hl":"1", "h2ht":"0", "pf":"103.2", "pa":"124.7", "vp":"0"},
        ]
    def trade_block(self):
        return {"0002": {"assets": ("16",), "wanted": "Wide receiver depth", "timestamp": ""}}
    def fantasy_schedule(self):
        return (
            MFLFantasyGame(1, ("0001", "0002"), (124.7, 103.2)),
            MFLFantasyGame(15, ("0001", "0002"), (None, None)),
        )
    def transactions(self, **kwargs):
        return (
            MFLTransaction("a1", "BBID_WAIVER", 1789200000, ("0001",), ("15",), ("8",)),
            MFLTransaction("a2", "TRADE", 1789100000, ("0001", "0002"), (), (), assets=("4", "5")),
        )
    def nfl_refresh_state(self, **kwargs): return {'active':False, 'next_kickoff':None}
    def scoring_rules(self): return {"positionRules": [{"positions":"QB|RB|WR|TE", "rule":[{"event":{"$t":"CC"},"points":{"$t":"*.5"},"range":{"$t":"0-99"}}]}]}
    def live_scoring(self, *, week):
        own = tuple(MFLLivePlayer(pid, 24.0 if pid == "1" else 0, "starter" if pid in OWN_STARTERS else "nonstarter", 0 if pid == "1" else 3600) for pid in sorted(OWN_ROSTER))
        other = tuple(MFLLivePlayer(pid, 19.4 if pid == "4" else 0, "starter" if pid in OTHER_STARTERS else "nonstarter", 0 if pid == "4" else 3600) for pid in sorted(OTHER_ROSTER))
        matchup = MFLLiveMatchup((
            MFLLiveFranchise("0001", 24, True, 4, 0, 14400, own),
            MFLLiveFranchise("0002", 19.4, False, 4, 0, 14400, other),
        ))
        return MFLLiveScoring(week, (matchup,))
    def named_players(self, ids): return [self._players[x] for x in ids]
    def lineup_settings(self):
        return MFLLineupSettings(5, (MFLLineupRule("QB",1,1), MFLLineupRule("RB",1,2), MFLLineupRule("WR",1,2), MFLLineupRule("TE",1,1)))
    def player_roster_statuses(self, ids, *, week=None): return dict(STATUSES)
    def nfl_team_kickoffs(self, *, week): return {"BUF": 1}
    def projected_scores(self, **kwargs): return dict(SCORES)
    def injuries(self, **kwargs): return {}
    def submit_lineup(self, *, week, starter_ids): return {"status":"test-only; nothing sent"}


web._client = lambda current, league: DemoClient()
web.projection_blend = lambda players, **kwargs: ProjectionBlend(dict(SCORES), dict(SCORES), {}, 0)
web.weekly_boxscore = lambda player, year, week: {
    "state": "Final",
    "stat_lines": ["24/34 C/ATT · 278 pass yds · 2 pass TD", "8 carries · 42 rush yds"],
    "categories": {
        "passing": {"passingYards": "278", "passingTouchdowns": "2"},
        "rushing": {"rushingYards": "42"},
    },
}
web.sessions["ui-test"] = web.BrowserSession(
    "not-a-real-cookie", 2026,
    [MFLLeague("demo", "0001", "Demo League · Test roster")], "ui-test-csrf"
)
app = web.app


@app.middleware("http")
async def test_session(request, call_next):
    request.scope["headers"] = [
        (key, value) for key, value in request.scope["headers"] if key != b"cookie"
    ] + [(b"cookie", b"wp_session=ui-test")]
    return await call_next(request)
