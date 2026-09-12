"""Isolated UI test server; synthetic data only, no MFL network or writes.

Run: python -m uvicorn ui_preview:app --app-dir tests --port 8766
"""
from types import SimpleNamespace

from weekly_projections.web import app as web
from weekly_projections.mfl.client import MFLPlayer, MFLLeague, MFLLineupSettings, MFLLineupRule, MFLLiveScoring, MFLLiveMatchup, MFLLiveFranchise, MFLLivePlayer
from weekly_projections.projection_sources import ProjectionBlend

PLAYERS = [
    MFLPlayer("1", "Allen, Josh", "QB", "BUF"),
    MFLPlayer("2", "Hurts, Jalen", "QB", "PHI"),
    MFLPlayer("3", "Barkley, Saquon", "RB", "PHI"),
    MFLPlayer("4", "Gibbs, Jahmyr", "RB", "DET"),
    MFLPlayer("5", "Chase, Ja'Marr", "WR", "CIN"),
    MFLPlayer("6", "Jefferson, Justin", "WR", "MIN"),
    MFLPlayer("7", "McBride, Trey", "TE", "ARI"),
    MFLPlayer("8", "Smith, DeVonta", "WR", "PHI"),
    MFLPlayer("9", "Henry, Derrick", "RB", "BAL"),
]
SCORES = dict(zip([p.id for p in PLAYERS], [24, 22, 21, 19, 22, 20, 15, 13, 16]))
STATUSES = {p.id: ("S" if p.id in {"1", "3", "5", "7", "8"} else "NS") for p in PLAYERS}


class DemoClient:
    config = SimpleNamespace(year=2026, franchise_id="0001", league_id="demo")
    session = None
    _players = {p.id: p for p in PLAYERS}
    def current_week(self): return 1
    def roster_ids(self): return set(self._players)
    def players(self): return dict(self._players)
    def franchise_names(self): return {"0001":"Test team", "0002":"Test opponent"}
    def franchise_roster(self, fid): return set(self._players)
    def trade_rosters(self): return {'0001':set(self._players), '0002':set(self._players)}
    def nfl_refresh_state(self, **kwargs): return {'active':False, 'next_kickoff':None}
    def scoring_rules(self): return {"positionRules": [{"positions":"QB|RB|WR|TE", "rule":[{"event":{"$t":"CC"},"points":{"$t":"*.5"},"range":{"$t":"0-99"}}]}]}
    def live_scoring(self, *, week):
        players = tuple(MFLLivePlayer(p.id, 24.0 if p.id == "1" else 0, "starter" if STATUSES[p.id] == "S" else "nonstarter", 0 if p.id == "1" else 3600) for p in PLAYERS)
        return MFLLiveScoring(week, (MFLLiveMatchup((MFLLiveFranchise("0001",24,True,4,0,14400,players), MFLLiveFranchise("0002",24,False,4,0,14400,players))),))
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
