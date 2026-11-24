from dataclasses import asdict

import pytest

from weekly_projections.defense_streaming import UNIT_RANKS_2026, rank_defense_streams
from weekly_projections.defense_talent import build_defense_talent, load_defense_talent
from tests.test_defense_streaming import defense, game


def season(weeks=4):
    teams = sorted(UNIT_RANKS_2026)
    stats, schedule = [], []
    for week in range(1, weeks + 1):
        for index in range(0, 32, 2):
            home, away = teams[index:index + 2]
            game_id = f"2026_{week:02}_{away}_{home}"
            schedule.append(dict(season="2026", week=str(week), game_type="REG", game_id=game_id,
                                 home_team=home, away_team=away, home_score=str(index + 2), away_score=str(index + 1)))
            for team, opponent, allowed_index in ((away, home, index), (home, away, index + 1)):
                stats.append(dict(season="2026", week=str(week), season_type="REG", game_id=game_id,
                                  team=team, opponent_team=opponent, rushing_yards=str(50 + allowed_index),
                                  passing_yards=str(120 + 2 * allowed_index), sack_yards_lost="20"))
    return stats, schedule


def set_allowed(stats, schedule, team, weeks, *, run, passing, points):
    for row in stats:
        if row["opponent_team"] == team and int(row["week"]) in weeks:
            row.update(rushing_yards=str(run), passing_yards=str(passing), sack_yards_lost="20")
    for game_row in schedule:
        if int(game_row["week"]) in weeks:
            if game_row["home_team"] == team:
                game_row["away_score"] = str(points)
            elif game_row["away_team"] == team:
                game_row["home_score"] = str(points)


def test_after_week_three_uses_completed_per_game_stats_and_net_passing():
    stats, games = season()
    assert build_defense_talent(stats, games, year=2026, before_week=3) == {}
    result = build_defense_talent(stats, games, year=2026, before_week=4)
    assert len(result) == 32
    assert result["ARI"].rank == result["ARI"].run_rank == result["ARI"].pass_rank == 1
    assert result["ARI"].points_rank == 1
    assert result["ARI"].run_allowed == 50
    assert result["ARI"].pass_allowed == 100  # gross 120 less 20 sack yards
    assert result["ARI"].points_allowed == 1
    assert result["ARI"].through_week == result["ARI"].games == 3
    assert result["WAS"].rank == 32
    # Published Week 4 cannot leak into the Week 4 plan.
    assert result == build_defense_talent(stats[:96], games, year=2026, before_week=4)


def test_delayed_or_incomplete_week_uses_last_complete_week():
    stats, games = season()
    result = build_defense_talent(stats[:-1], games, year=2026, before_week=5)
    assert all(value.through_week == 3 for value in result.values())
    games[-1]["away_score"] = ""
    assert build_defense_talent(stats, games, year=2026, before_week=5) == result
    assert build_defense_talent(stats[:32], games, year=2026, before_week=5) == {}


def test_byes_do_not_penalize_per_game_ranks_and_ties_share_ranks():
    stats, games = season(4)
    bye = games[-1]["game_id"]
    games = [g for g in games if g["game_id"] != bye]
    stats = [s for s in stats if s["game_id"] != bye]
    result = build_defense_talent(stats, games, year=2026, before_week=5)
    assert result["WAS"].games == 3 and result["ARI"].games == 4
    assert result["WAS"].run_rank == 32
    for row in stats:
        row.update(rushing_yards="100", passing_yards="200", sack_yards_lost="0")
    for row in games:
        row.update(home_score="20", away_score="20")
    tied = build_defense_talent(stats, games, year=2026, before_week=5)
    assert {value.rank for value in tied.values()} == {16.5}


def test_season_stats_replace_only_defense_talent_and_never_fantasy_points():
    stats, games = season()
    talent = {team: asdict(row) for team, row in build_defense_talent(stats, games, year=2026, before_week=4).items()}
    row = rank_defense_streams([defense("ARI")], year=2026, week=4, talent=talent,
                               games={"ARI": game("MIA")}, opponent_strength={}, now=100)[0]
    assert row.talent_rank == 1 and row.offense_rank == 32
    assert row.score == 100 and row.item.projection == 8
    assert row.talent_stats["through_week"] == 3
    missing = rank_defense_streams([defense("ARI")], year=2026, week=4, talent={},
                                   games={"ARI": game("MIA")}, opponent_strength={}, now=100)[0]
    assert missing.talent_rank is missing.score is None
    assert "not reused after Week 3" in missing.explanation


def test_bad_numeric_stat_is_not_zero_and_aliases_work():
    stats, games = season()
    for row in stats:
        if row["team"] == "HOU":
            row["team"] = "HST"
    assert len(build_defense_talent(stats, games, year=2026, before_week=4)) == 32
    stats[0]["passing_yards"] = "NaN"
    assert build_defense_talent(stats, games, year=2026, before_week=4) == {}


def test_loader_is_bounded_and_uses_verified_sources(monkeypatch):
    from weekly_projections import defense_talent as module
    stats, games = season()
    urls = []
    def read(url):
        urls.append(url)
        return stats if "stats_team_week_2026.csv" in url else games
    monkeypatch.setattr(module, "_csv_rows", read)
    assert len(load_defense_talent(2026, 4)) == 32
    assert len(urls) == 2
    assert load_defense_talent(2026, 3) == {}
    assert len(urls) == 2


def test_download_timeout_and_size_bounds(monkeypatch):
    from weekly_projections import defense_talent as module
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def raise_for_status(self): pass
        def iter_content(self, chunk_size):
            yield b"x" * (8 * 1024 * 1024 + 1)
    def get(url, **kw):
        assert kw == {"timeout": (5, 15), "stream": True}
        return Response()
    monkeypatch.setattr(module.requests, "get", get)
    with pytest.raises(ValueError, match="download bounds"):
        module._csv_rows("https://synthetic.invalid/public-stats.csv")


def test_late_season_improvement_uses_last_three_games_across_a_bye():
    stats, games = season(7)
    bye = next(g["game_id"] for g in games if g["week"] == "4" and "DET" in {g["home_team"], g["away_team"]})
    stats = [row for row in stats if row["game_id"] != bye]
    games = [row for row in games if row["game_id"] != bye]
    set_allowed(stats, games, "DET", {1, 2, 3}, run=200, passing=350, points=30)
    set_allowed(stats, games, "DET", {5, 6, 7}, run=25, passing=90, points=3)
    result = build_defense_talent(stats, games, year=2026, before_week=8)["DET"]
    assert result.games == 6
    recent = result.recent
    assert recent.weeks == (5, 6, 7)
    assert recent.run_allowed == 25 and recent.pass_allowed == 70 and recent.points_allowed == 3
    assert recent.previous_games == 3
    assert recent.run_change == -175 and recent.pass_change == -260 and recent.points_change == -27
    assert recent.rank < result.rank and recent.direction == "up"
    assert recent.rank_change == result.rank - recent.rank


def test_declining_recent_form_and_independent_previous_three_window():
    stats, games = season(9)
    set_allowed(stats, games, "DET", {1, 2, 3}, run=300, passing=450, points=40)
    set_allowed(stats, games, "DET", {4, 5, 6}, run=100, passing=220, points=20)
    set_allowed(stats, games, "DET", {7, 8, 9}, run=50, passing=120, points=10)
    set_allowed(stats, games, "HOU", {1, 2, 3, 4, 5, 6}, run=10, passing=50, points=1)
    set_allowed(stats, games, "HOU", {7, 8, 9}, run=250, passing=400, points=40)
    result = build_defense_talent(stats, games, year=2026, before_week=10)
    detroit, houston = result["DET"].recent, result["HOU"].recent
    assert detroit.weeks == (7, 8, 9)
    assert detroit.run_change == -50 and detroit.pass_change == -100 and detroit.points_change == -10
    assert houston.direction == "down" and houston.rank_change < 0
    assert houston.run_change > 0 and houston.pass_change > 0 and houston.points_change > 0


def test_short_history_does_not_invent_a_previous_window_and_ties_remain_equal():
    stats, games = season(5)
    for row in stats:
        row.update(rushing_yards="100", passing_yards="200", sack_yards_lost="20")
    for row in games:
        row.update(home_score="20", away_score="20")
    first = build_defense_talent(stats, games, year=2026, before_week=4)["DET"].recent
    assert first.direction is None and first.weeks == (1, 2, 3)
    assert first.previous_games == 0 and first.run_change is first.pass_change is first.points_change is None
    later = build_defense_talent(stats, games, year=2026, before_week=6)["DET"].recent
    assert later.direction == "steady" and later.rank_change == 0 and later.rank == 16.5
    assert later.previous_games == 0 and later.run_change is None


def test_recent_window_excludes_future_unpublished_and_postseason_games():
    stats, games = season(7)
    set_allowed(stats, games, "DET", {7}, run=1, passing=20, points=0)
    before = build_defense_talent(stats, games, year=2026, before_week=7)["DET"]
    assert before.recent.weeks == (4, 5, 6)
    missing = stats[:-1]  # An incomplete latest week must never form part of a trend.
    assert build_defense_talent(missing, games, year=2026, before_week=8)["DET"] == before
    for row in stats:
        if row["week"] == "7":
            row["season_type"] = "POST"
    assert build_defense_talent(stats, games, year=2026, before_week=8)["DET"] == before


def test_recent_rank_does_not_change_season_stream_fit_or_projection():
    stats, games = season(6)
    set_allowed(stats, games, "DET", {1, 2, 3}, run=200, passing=350, points=30)
    set_allowed(stats, games, "DET", {4, 5, 6}, run=25, passing=90, points=3)
    talent = {team: asdict(row) for team, row in build_defense_talent(stats, games, year=2026, before_week=7).items()}
    without_recent = {team: {key: value for key, value in row.items() if key != "recent"} for team, row in talent.items()}
    args = dict(year=2026, week=7, games={"DET": game("MIA")}, opponent_strength={}, now=100)
    new = rank_defense_streams([defense("DET")], talent=talent, **args)[0]
    old = rank_defense_streams([defense("DET")], talent=without_recent, **args)[0]
    assert new.score == old.score and new.item.projection == old.item.projection == 8
    assert new.talent_stats["recent"]["direction"] == "up"
