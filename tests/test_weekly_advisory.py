from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

import pytest

from weekly_projections import weekly_advisory as module
from weekly_projections.mfl.client import MFLAvailability, MFLPlayer
from weekly_projections.recommendations import PlayerRecommendation
from tests.test_defense_streaming import defense, game


def epoch(value):
    return datetime.fromisoformat(value).replace(tzinfo=module.EASTERN).timestamp()


class Store:
    def __init__(self):
        self.records = {}

    def load_league_report_snapshot(self, owner, **kw):
        value = self.records.get((owner, kw["year"], kw["league_id"], kw["report_type"], kw.get("report_key", "")))
        return {"payload": deepcopy(value)} if value else None

    def save_league_report_snapshot(self, owner, **kw):
        self.records[(owner, kw["year"], kw["league_id"], kw["report_type"], kw.get("report_key", ""))] = deepcopy(kw["payload"])

    def load_weekly_advisory_tasks(self, *, cycle, limit):
        return [dict(scope_hash=key[0], year=key[1], league_id=key[2], payload=deepcopy(value))
                for key, value in self.records.items() if key[3] == module.REPORT
                and value.get("attempt_cycle", value["cycle"]) != cycle][:limit]

    def finish_weekly_advisory_task(self, task, payload):
        key = (task["scope_hash"], task["year"], task["league_id"], module.REPORT, "")
        if self.records[key] != task["payload"]:
            return False
        self.records[key] = deepcopy(payload)
        return True


def inputs():
    own = defense("DET", owned=True)
    candidate = defense("HOU", locked=True)
    add = PlayerRecommendation(MFLPlayer("add", "Waiver WR", "WR", "BUF"),
                               MFLAvailability("add", status="waiver"), 15, 5,
                               own.player, "Upgrade", "good", "")
    return {"board": [asdict(item) for item in [own, candidate, add]],
            "games": {"HOU": game("MIA", kickoff=4_000_000_000), "DET": game("BUF", kickoff=4_000_000_000)},
            "strength": {}, "locked": []}


def funding():
    return dict(balance=25, activity=[], catalog={}, pricing_error=None)


def fail_prepare():
    raise AssertionError("Cached visits must not load bid history/budget or recompute")


@pytest.mark.parametrize("now,expected", [
    ("2026-11-24T01:59:59", "2026-11-17T02:00:00-05:00"),
    ("2026-11-24T02:00:00", "2026-11-24T02:00:00-05:00"),
    ("2026-07-07T02:00:00", "2026-07-07T02:00:00-04:00"),
    ("2026-11-02T12:00:00", "2026-10-27T02:00:00-04:00"),
])
def test_tuesday_two_am_eastern_handles_dst(now, expected):
    assert module.weekly_cycle(epoch(now)).isoformat() == expected


def test_visits_restart_and_concurrent_visits_reuse_once_weekly_result(monkeypatch):
    store = Store()
    cache = module.WeeklyAdvisoryCache(lambda: store, lambda *args: None)
    calls = []
    original = module.compute_plan
    def compute(*args, **kw):
        calls.append(kw["week"])
        return original(*args, **kw)
    monkeypatch.setattr(module, "compute_plan", compute)
    now = epoch("2026-11-23T12:00:00")
    first = cache.resolve("owner", 2026, "123", 2, inputs=inputs(), prepare=funding, now=now)
    changed = inputs()
    changed["board"][2]["projection"] = 30
    for _ in range(3):
        saved = cache.resolve("owner", 2026, "123", 2, inputs=changed, prepare=fail_prepare, now=now + 60)
        assert saved["plan"] == first["plan"]
    restarted = module.WeeklyAdvisoryCache(lambda: store, lambda *args: None)
    assert restarted.resolve("owner", 2026, "123", 2, inputs=changed,
                             prepare=fail_prepare, now=now + 120)["plan"] == first["plan"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: restarted.resolve("owner", 2026, "123", 3,
                               inputs=changed, prepare=funding, now=now + 180), range(2)))
    assert calls == [2, 3]
    assert results[0]["plan"] == results[1]["plan"]


def test_timer_runs_without_sessions_once_and_uses_latest_saved_inputs():
    store = Store()
    errors = []
    cache = module.WeeklyAdvisoryCache(lambda: store, lambda *args: errors.append(args))
    monday = epoch("2026-11-23T12:00:00")
    cache.resolve("owner", 2026, "123", 2, inputs=inputs(), prepare=funding, now=monday)
    changed = inputs()
    changed["board"][2]["projection"] = 30
    cache.resolve("owner", 2026, "123", 2, inputs=changed, prepare=fail_prepare, now=monday + 60)
    # A new process has no MFL session and never reads MFL or submits a claim.
    worker = module.WeeklyAdvisoryCache(lambda: store, lambda *args: errors.append(args))
    assert worker.run_due(now=epoch("2026-11-24T01:59:59")) == 0
    assert worker.run_due(now=epoch("2026-11-24T02:00:00")) == 1
    assert worker.run_due(now=epoch("2026-11-24T02:10:00")) == 0
    saved = worker.resolve("owner", 2026, "123", 2, inputs=changed, prepare=fail_prepare,
                           now=epoch("2026-11-24T03:00:00"))
    assert saved["plan"]["queue"]["items"][0]["projection"] == 30
    assert errors == []


def test_cache_is_private_per_owner_league_year_and_new_week():
    cache = module.WeeklyAdvisoryCache(lambda: None, lambda *args: None)
    for owner, year, league in [("first", 2026, "1"), ("second", 2026, "1"), ("first", 2026, "2"), ("first", 2025, "1")]:
        result = cache.resolve(owner, year, league, 2, inputs=inputs(), prepare=funding)
        assert result["week"] == 2
    assert len(cache.memory) == 4


def test_saved_queue_filters_ownership_and_locks_without_reoptimizing():
    cache = module.WeeklyAdvisoryCache(lambda: None, lambda *args: None)
    seed = inputs()
    saved = cache.resolve("owner", 2026, "1", 2, inputs=seed, prepare=funding)
    board = module._board(seed)
    assert len(module.plan_context(saved, board, set(), now=0)["waiver_queue"].items) == 1
    assert not module.plan_context(saved, board, {"DET"}, now=0)["waiver_queue"].items
    board[2] = replace(board[2], fantasy_team_id="0002")
    assert not module.plan_context(saved, board, set(), now=0)["waiver_queue"].items
    board[1] = replace(board[1], fantasy_team_id="0002")
    assert not module.plan_context(saved, board, set(), now=0)["defense_targets"]


def test_global_talent_loaded_once_shared_across_leagues_and_restart(monkeypatch):
    store = Store()
    calls = []
    monkeypatch.setattr(module, "load_defense_talent", lambda year, week: calls.append((year, week)) or {})
    for _ in range(2):
        cache = module.WeeklyAdvisoryCache(lambda: store, lambda *args: None)
        for league in ["1", "2"]:
            cache.resolve("owner", 2026, league, 10, inputs=inputs(), prepare=funding)
    assert calls == [(2026, 10)]


def test_source_failure_is_honest_and_does_not_repeat_on_visits(monkeypatch):
    errors = []
    def unavailable(*args):
        raise ValueError("Synthetic unavailable stats")
    monkeypatch.setattr(module, "load_defense_talent", unavailable)
    cache = module.WeeklyAdvisoryCache(lambda: None, lambda *args: errors.append(args[0]))
    first = cache.resolve("owner", 2026, "1", 10, inputs=inputs(), prepare=funding)
    second = cache.resolve("owner", 2026, "1", 10, inputs=inputs(), prepare=fail_prepare)
    assert errors == ["defense_talent_unavailable"]
    assert first["plan"] == second["plan"]
    assert all(row["talent_rank"] is None for row in first["plan"]["streams"])


def test_lifespan_starts_and_stops_weekly_worker(monkeypatch):
    import asyncio
    from weekly_projections.web import app as web_app
    events = []
    class Worker:
        def __init__(self, **kw):
            assert kw["target"] is web_app._weekly_advisory_loop
        def start(self): events.append("start")
        def join(self, **kw): events.append("stop")
    monkeypatch.setenv("WP_DATABASE_URL", "synthetic-configured-url")
    monkeypatch.setattr(web_app, "Thread", Worker)
    async def lifecycle():
        async with web_app._app_lifespan(web_app.app):
            assert events == ["start"]
    asyncio.run(lifecycle())
    assert events == ["start", "stop"]


def test_postgres_worker_queries_are_bounded_and_preserve_scope(monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from weekly_projections.web.session_store import EncryptedSessionStore
    statements = []
    scope = b"a" * 32
    original = {"version": 1, "week": 12, "cycle": "2026-11-17"}
    class Connection:
        def execute(self, sql, params):
            statements.append((sql, params))
            return SimpleNamespace(fetchall=lambda: [(scope, 2026, "123", deepcopy(original))], rowcount=1)
    @contextmanager
    def connection():
        yield Connection()
    store = EncryptedSessionStore.__new__(EncryptedSessionStore)
    store.database_url = "synthetic-only"
    monkeypatch.setattr(store, "_connect_postgres", connection)
    tasks = store.load_weekly_advisory_tasks(cycle="2026-11-24", limit=1000)
    assert len(tasks) == 1
    sql, params = statements[-1]
    assert "report_key = 'weekly-advisory'" in sql and "LIMIT %s" in sql
    assert params == ("2026-11-24", 100)
    assert store.finish_weekly_advisory_task(tasks[0], {**original, "cycle": "2026-11-24"})
    sql, params = statements[-1]
    assert "scope_hash = %s AND season = %s AND league_id = %s" in sql
    assert "AND payload = %s::jsonb" in sql
    assert params[1:4] == (scope, 2026, "123")


def test_active_refresh_uses_only_registered_unexpired_logins(monkeypatch):
    from types import SimpleNamespace
    from weekly_projections.web import app as web_app
    from weekly_projections.mfl.client import MFLLeague
    league = MFLLeague("123", "0001", "League")
    active = web_app.BrowserSession("private-cookie", 2026, [league], "csrf", owner_fingerprint="owner")
    expired = web_app.BrowserSession("expired-cookie", 2026, [league], "csrf", owner_fingerprint="expired", expires_at=0)
    monkeypatch.setattr(web_app, "sessions", {"active": active, "expired": expired})
    monkeypatch.setattr(web_app, "weekly_advisories", SimpleNamespace(registered=lambda *key: key[0] == "owner"))
    monkeypatch.setattr(web_app, "_client", lambda *args: "synthetic-client")
    calls = []
    monkeypatch.setattr(web_app, "_market_enrichment_context", lambda *args, **kwargs: calls.append((args, kwargs)))
    web_app.weekly_worker_stop.clear()
    web_app._refresh_active_weekly_inputs()
    assert len(calls) == 1 and calls[0][0][0] is active
    assert calls[0][1] == {"scheduled": True}


def test_market_api_renders_season_stats_and_reuses_saved_plan(monkeypatch):
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from weekly_projections.web import app as web_app
    from weekly_projections.mfl.client import MFLLeague, MFLLeagueDetails, MFLFranchise
    from weekly_projections.projection_sources import ProjectionBlend
    from weekly_projections.defense_talent import build_defense_talent
    from tests.test_defense_talent import season
    stats, games = season()
    talent = {team: asdict(row) for team, row in build_defense_talent(stats, games, year=2026, before_week=4).items()}
    cache = module.WeeklyAdvisoryCache(lambda: None, lambda *args: None)
    monkeypatch.setattr(cache, "_talent", lambda *args: talent)
    monkeypatch.setattr(web_app, "weekly_advisories", cache)
    league = MFLLeague("123", "0001", "League")
    current = web_app.BrowserSession("synthetic", 2026, [league], "csrf", owner_fingerprint="owner")
    monkeypatch.setattr(web_app, "sessions", {"weekly-page": current})
    board = module._board(inputs())
    current.player_catalog = {item.player.id: item.player for item in board}
    calls = []
    def details():
        calls.append("budget")
        return MFLLeagueDetails((), {"0001": MFLFranchise("0001", "Owner", faab_balance=25)})
    client = SimpleNamespace(week_games=inputs()["games"], opponent_strength={},
                             league_details=details, transactions=lambda **kwargs: ())
    monkeypatch.setattr(web_app, "_client", lambda *args: client)
    monkeypatch.setattr(web_app, "_remember_catalog", lambda *args: None)
    monkeypatch.setattr(web_app, "_load_player_board", lambda *args, **kw: (
        13, [board[0].player], board,
        ProjectionBlend(scores={}, mfl_scores={}, ml_scores={}, ml_matched=0), set(),
    ))
    browser = TestClient(web_app.app)
    browser.cookies.set("wp_session", "weekly-page")
    for _ in range(2):
        response = browser.get("/api/player-market/enrichment?league=123")
        assert response.status_code == 200
        data = response.json()
        assert "Through Week 3" in data["defense_html"]
        assert "Run: #" in data["defense_html"] and "Net pass: #" in data["defense_html"]
        assert "Total points allowed:" in data["defense_html"]
        assert "Last 3 games" in data["defense_html"] and "First three games" in data["defense_html"]
        assert "Six completed games are needed" in data["defense_html"]
        assert "Tuesdays at 2:00 a.m. Eastern" in data["waiver_html"]
        assert "Budget/history snapshot" in data["waiver_html"]
    assert calls == ["budget"]


def test_weekly_failure_does_not_spin_and_next_cycle_can_recover(monkeypatch):
    store = Store()
    errors = []
    cache = module.WeeklyAdvisoryCache(lambda: store, lambda *args: errors.append(args[0]))
    cache.resolve("owner", 2026, "1", 2, inputs=inputs(), prepare=funding,
                  now=epoch("2026-11-23T12:00:00"))
    original = module.compute_plan
    def fail(*args, **kw):
        raise ValueError("Synthetic computation failure")
    monkeypatch.setattr(module, "compute_plan", fail)
    assert cache.run_due(now=epoch("2026-11-24T02:00:00")) == 0
    assert cache.run_due(now=epoch("2026-11-24T02:01:00")) == 0
    assert errors == ["weekly_advisory_compute_failed"]
    monkeypatch.setattr(module, "compute_plan", original)
    assert cache.run_due(now=epoch("2026-12-01T02:00:00")) == 1
    assert cache.run_due(now=epoch("2026-12-01T02:01:00")) == 0


def test_database_outage_does_not_discard_successful_public_stats(monkeypatch):
    class UnavailableCache(Store):
        def load_league_report_snapshot(self, owner, **kw):
            if kw["report_type"] == "defense-talent":
                raise ValueError("Synthetic database outage")
            return super().load_league_report_snapshot(owner, **kw)
        def save_league_report_snapshot(self, owner, **kw):
            if kw["report_type"] == "defense-talent":
                raise ValueError("Synthetic database outage")
            return super().save_league_report_snapshot(owner, **kw)
    talent = {"HOU": {"rank": 1, "through_week": 3}}
    monkeypatch.setattr(module, "load_defense_talent", lambda *args: talent)
    errors = []
    store = UnavailableCache()
    cache = module.WeeklyAdvisoryCache(lambda: store, lambda *args: errors.append(args[0]))
    saved = cache.resolve("owner", 2026, "1", 4, inputs=inputs(), prepare=funding)
    houston = next(row for row in saved["plan"]["streams"] if row["player_id"] == "HOU")
    assert houston["talent_rank"] == 1
    assert errors == ["defense_talent_cache_load_failed", "defense_talent_cache_save_failed"]


def test_older_plan_and_public_talent_cache_upgrade_once(monkeypatch):
    store = Store()
    old = module.WeeklyAdvisoryCache(lambda: store, lambda *args: None)
    now = epoch("2026-11-24T03:00:00")
    old.resolve("owner", 2026, "1", 2, inputs=inputs(), prepare=funding, now=now)
    key = ("owner", 2026, "1", module.REPORT, "")
    store.records[key]["version"] = 1
    calls = []
    def prepare():
        calls.append("upgrade")
        return funding()
    current = module.WeeklyAdvisoryCache(lambda: store, lambda *args: None)
    current.resolve("owner", 2026, "1", 2, inputs=inputs(), prepare=prepare, now=now)
    current.resolve("owner", 2026, "1", 2, inputs=inputs(), prepare=fail_prepare, now=now + 60)
    assert calls == ["upgrade"]
    assert store.records[key]["version"] == module.VERSION
    # Existing public season-only snapshots must not hide the new recent data.
    store.save_league_report_snapshot("public-defense-talent-v1", year=2026, league_id="0",
                                     report_type="defense-talent", report_key="7:2026-11-24",
                                     payload={"DET": {"rank": 20}})
    public_calls = []
    monkeypatch.setattr(module, "load_defense_talent", lambda *args: public_calls.append(args) or {})
    current._talent(2026, 7, now)
    current._talent(2026, 7, now)
    assert public_calls == [(2026, 7)]
