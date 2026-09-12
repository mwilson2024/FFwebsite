from weekly_projections.league_intelligence import (
    build_power_rankings,
    build_recap,
    playoff_probability,
    rest_of_season_pace,
    waiver_trends,
)
from weekly_projections.mfl.client import MFLFantasyGame, MFLTransaction


def _games():
    return (
        MFLFantasyGame(1, ("0001", "0002"), (120.0, 90.0)),
        MFLFantasyGame(1, ("0003", "0004"), (110.0, 100.0)),
        MFLFantasyGame(2, ("0001", "0003"), (80.0, 130.0)),
        MFLFantasyGame(2, ("0002", "0004"), (105.0, 95.0)),
        MFLFantasyGame(3, ("0001", "0004"), (0.0, 0.0)),
    )


def test_power_rankings_luck_and_current_week_exclusion():
    names = {f"000{i}": f"Team {i}" for i in range(1, 5)}
    rankings = build_power_rankings(_games(), names, current_week=3)
    assert {row.franchise_id for row in rankings} == set(names)
    assert [row.rank for row in rankings] == [1, 2, 3, 4]
    assert all(row.wins + row.losses + row.ties == 2 for row in rankings)
    assert any(row.luck != 0 for row in rankings)
    assert rankings[0].power_score >= rankings[-1].power_score
    assert build_power_rankings((), names, current_week=1) == ()


def test_trends_recap_probability_and_ros_pace():
    activity = (
        MFLTransaction("1", "WAIVER", 2, ("0001",), ("10",), ("20",)),
        MFLTransaction("2", "FREE_AGENT", 1, ("0002",), ("10",), ()),
    )
    assert waiver_trends(activity)[0].net == 2
    names = {f"000{i}": f"Team {i}" for i in range(1, 5)}
    rankings = build_power_rankings(_games(), names, current_week=3)
    recap = build_recap(_games(), names, current_week=3)
    assert recap.week == 2
    assert "Week 2" in recap.headline
    assert playoff_probability(rankings[0], rankings[-1])[0] > 50
    assert rest_of_season_pace(10.0, week=10, end_week=17) == 80.0
