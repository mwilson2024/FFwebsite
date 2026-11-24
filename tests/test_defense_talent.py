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
