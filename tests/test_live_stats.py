from dataclasses import replace
from types import SimpleNamespace

import pytest
import requests
from fastapi.testclient import TestClient

from weekly_projections.live_stats import parse_boxscore, scoring_components
from weekly_projections.mfl.client import MFLPlayer, MFLLeague
from weekly_projections.web import app as web


def sample_box():
    return {
        "header": {"week": 1, "season": {"year": 2026, "type": 2}, "competitions": [{"status": {"type": {"state": "post", "completed": True}}}]},
        "boxscore": {"players": [{"statistics": [{"name": "receiving", "keys": ["receptions", "receivingYards", "receivingTouchdowns"], "athletes": [{"athlete": {"id": "123"}, "stats": ["7", "110", "1"]}]}]}]},
    }


def sample_rules():
    return {"positionRules": {"positions": "WR|TE", "rule": [
        {"event": {"$t": "CC"}, "range": {"$t": "0-99"}, "points": {"$t": "*.5"}},
        {"event": "CY", "range": "0-999", "points": "*.1"},
        {"event": "CY", "range": "100-199", "points": "5"},
        {"event": "#C", "range": "0-10", "points": "*6"},
        {"event": "FG", "range": "50-99", "points": "5"},
    ]}}


def test_stat_line_is_exact_player_week_season_and_game_state():
    player = MFLPlayer("p", "Player", "WR", "SEA", "123")
    data = sample_box()
    parsed = parse_boxscore(data, player, 2026, 1)
    assert parsed["state"] == "Final"
    assert parsed["stat_lines"] == ["7 rec · 110 rec yds · 1 rec TD"]
    assert parse_boxscore(data, player, 2025, 1) is None
    assert parse_boxscore(data, player, 2026, 2) is None
    assert parse_boxscore(data, replace(player, espn_id="999"), 2026, 1) is None
    data["header"]["competitions"][0]["status"]["type"] = {"state": "in", "completed": False}
    assert parse_boxscore(data, player, 2026, 1)["state"] == "Live"
    data["header"]["competitions"][0]["status"]["type"]["state"] = "pre"
    assert parse_boxscore(data, player, 2026, 1) is None


def test_league_rules_include_receptions_yards_td_and_bonus_not_generic_ppr():
    box = parse_boxscore(sample_box(), MFLPlayer("p", "Player", espn_id="123"), 2026, 1)
    components = scoring_components(box, sample_rules(), "WR")
    assert [row["points"] for row in components] == [3.5, 11, 5, 6]
    assert scoring_components(box, sample_rules(), "QB") == []
    assert scoring_components({"categories": {}}, sample_rules(), "WR") == []


def scoring_client(monkeypatch, status="starter", seconds=0):
    player = MFLPlayer("p", "Starter", "WR", "SEA", "123")
    item = SimpleNamespace(player_id="p", status=status, score=27.2, game_seconds_remaining=seconds)
    league = MFLLeague("l", "0001", "League")
    live = SimpleNamespace(matchups=[SimpleNamespace(franchises=[SimpleNamespace(franchise_id="0001", players=[item])])])
    fake = SimpleNamespace(live_scoring=lambda **kw: live, players=lambda: {"p": player}, scoring_rules=sample_rules)
    monkeypatch.setattr(web, "_client", lambda *args: fake)
    monkeypatch.setattr(web, "sessions", {"test": web.BrowserSession("fake", 2026, [league], "csrf")})
    client = TestClient(web.app)
    client.cookies.set("wp_session", "test")
    return client


def test_endpoint_reconciles_official_score_without_inventing_events(monkeypatch):
    client = scoring_client(monkeypatch)
    box = parse_boxscore(sample_box(), MFLPlayer("p", "Player", espn_id="123"), 2026, 1)
    monkeypatch.setattr(web, "weekly_boxscore", lambda *args: box)
    response = client.get("/api/scoring/p?league=l&franchise=0001&week=1")
    assert response.status_code == 200
    data = response.json()
    assert data["official_points"] == 27.2
    assert data["difference"] == 1.7
    assert data["state"] == "Final"
    assert sum(c["points"] for c in data["components"]) + data["difference"] == pytest.approx(27.2)


@pytest.mark.parametrize("status", ["nonstarter", "bench", "IR", "R"])
def test_bench_or_unknown_saved_status_cannot_fetch_details(monkeypatch, status):
    client = scoring_client(monkeypatch, status=status)
    monkeypatch.setattr(web, "weekly_boxscore", lambda *a: pytest.fail("Bench must not fetch stats"))
    assert client.get("/api/scoring/p?league=l&franchise=0001&week=1").status_code == 409


def test_details_validate_league_franchise_week_and_session(monkeypatch):
    client = scoring_client(monkeypatch)
    assert client.get("/api/scoring/p?league=l&franchise=0002&week=1").status_code == 409
    assert client.get("/api/scoring/p?league=l&franchise=0001&week=19").status_code == 400
    assert client.get("/api/scoring/p?league=other&franchise=0001&week=1").status_code == 404
    client.cookies.clear()
    assert client.get("/api/scoring/p?league=l&franchise=0001&week=1", follow_redirects=False).status_code in {303, 401}


def test_unavailable_stats_preserve_mfl_score_and_upcoming_does_not_fetch(monkeypatch):
    client = scoring_client(monkeypatch, seconds=3600)
    monkeypatch.setattr(web, "weekly_boxscore", lambda *a: pytest.fail("No future boxscore fetch"))
    data = client.get("/api/scoring/p?league=l&franchise=0001&week=1").json()
    assert data["state"] == "Upcoming" and data["stat_lines"] == []
    client = scoring_client(monkeypatch)
    def unavailable(*args):
        raise requests.Timeout("private error text must not reach the browser")
    monkeypatch.setattr(web, "weekly_boxscore", unavailable)
    data = client.get("/api/scoring/p?league=l&franchise=0001&week=1").json()
    assert data["official_points"] == 27.2
    assert "unavailable" in data["note"] and "private error" not in str(data)


def test_bench_excluded_from_totals_counts_state_and_detail_controls():
    starter = web.LivePlayerView(MFLPlayer("s", "Saved Starter"), 20, "starter", 0)
    bench = web.LivePlayerView(MFLPlayer("b", "Bench Player"), 99, "nonstarter", 100)
    team = web.LiveTeamView("0001", "Team", 20, False, 7, 3, (starter, bench))
    assert team.starter_points == 20 and team.score_difference == 0
    assert team.starters_playing == team.starters_left == 0
    matchup = web.HeadToHeadView((team,), 1, 1)
    assert matchup.game_state == "Final"
    context = {"paired_rows": [(starter, None, "QB"), (bench, None, "QB")], "head_to_head": matchup, "league": MFLLeague("l", "0001", "League"), "week": 1}
    html = web.templates.env.get_template("_matchup_rows.html").render(context)
    assert 'data-scoring-player="s"' in html
    assert 'data-scoring-player="b"' not in html


@pytest.mark.parametrize("seconds,state,label", [(900, "live", "Playing"), (3600, "upcoming", "Yet to play"), (0, "final", "Final")])
def test_player_status_has_color_class_and_readable_label(seconds, state, label):
    player = web.LivePlayerView(MFLPlayer("b", "Bench Player"), 0, "nonstarter", seconds)
    html = web.templates.env.get_template("_matchup_rows.html").render(
        paired_rows=[(player, None, "BN")], league=MFLLeague("l", "0001", "League"), week=1,
    )
    assert f'data-game-state="{state}"' in html
    assert f'class="game-status-badge {state}">{label}</span>' in html
    assert "data-scoring-player" not in html


def test_matchup_header_switches_all_leagues_and_separates_stats_from_points():
    from bs4 import BeautifulSoup
    league = MFLLeague('1', '0001', 'First League')
    session = web.BrowserSession('fake', 2026, [league, MFLLeague('2', '0002', 'Second League')], 'csrf')
    player = web.LivePlayerView(MFLPlayer('p', 'Player', 'QB'), 0, 'starter', 3600)
    team = web.LiveTeamView('0001', 'Team', 0, False, 1, 0, (player,))
    html = web.templates.env.get_template('scores.html').render(
        league=league, session=session, week=1, current_week=1, weeks=range(1,19),
        head_to_head=web.HeadToHeadView((team,),1,1), live=None, error=None, refresh_seconds=60,
    )
    soup = BeautifulSoup(html, 'html.parser')
    assert len(soup.select('select[name="league"]')) == 1
    switcher = soup.select_one('header .header-league-picker')
    assert [o['value'] for o in switcher.select('option')] == ['1','2']
    assert switcher.select_one('input[name="week"]')['value'] == '1'
    assert soup.select_one('.week-picker input[name="league"]')['value'] == '1'
    identity = soup.select_one('.matchup-identity-line')
    assert identity.select_one('.player-identity') and identity.select_one('.game-status-badge')
    panel = soup.select_one('.player-scoring-panel')
    assert panel.select_one('.player-stat-line') is not None
    assert panel.select_one('details .player-stat-line') is None
    trigger = panel.select_one('.stat-line-heading .points-trigger')
    assert trigger.get_text().startswith('Points breakdown')
    assert trigger['data-stat-line'] == panel.select_one('.player-stat-line')['id']
    assert trigger['aria-controls'] == 'points-card'
    assert soup.select_one('dialog#points-card')
    assert panel.select_one('details') is None
