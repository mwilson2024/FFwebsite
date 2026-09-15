from weekly_projections.league_intelligence import (
    build_local_playoff_games,
    build_projected_playoff_rounds,
    build_playoff_seeds,
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


def test_power_ranking_copy_uses_actual_completed_week_span():
    names = {"0001": "One", "0002": "Two"}
    rankings = build_power_rankings(
        (MFLFantasyGame(1, ("0001", "0002"), (120.0, 90.0)),),
        names,
        current_week=2,
    )
    assert rankings[0].form_weeks == 1
    assert rankings[0].one_liner == "A strong opening week has this team near the top."
    assert "three weeks" not in rankings[0].one_liner


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


def test_local_playoff_seeding_puts_three_division_leaders_first():
    rows = [
        {"id":"0001","h2hw":"8","h2hl":"2","h2ht":"0","pf":"900"},
        {"id":"0002","h2hw":"9","h2hl":"1","h2ht":"0","pf":"950"},
        {"id":"0003","h2hw":"7","h2hl":"3","h2ht":"0","pf":"850"},
        {"id":"0004","h2hw":"10","h2hl":"0","h2ht":"0","pf":"1000"},
        {"id":"0005","h2hw":"6","h2hl":"4","h2ht":"0","pf":"800"},
        {"id":"0006","h2hw":"5","h2hl":"5","h2ht":"0","pf":"700"},
        {"id":"0007","h2hw":"4","h2hl":"6","h2ht":"0","pf":"650"},
        {"id":"0008","h2hw":"3","h2hl":"7","h2ht":"0","pf":"600"},
        {"id":"0009","h2hw":"2","h2hl":"8","h2ht":"0","pf":"500"},
    ]
    divisions = {"0001":"a","0004":"a","0002":"b","0005":"b","0003":"c","0006":"c","0007":"c","0008":"c","0009":"c"}
    seeds = build_playoff_seeds(rows, divisions, ("a", "b", "c"))
    assert seeds[:3] == ("0004", "0002", "0003")
    assert seeds[3:5] == ("0001", "0005")
    games = build_local_playoff_games(seeds, first_playoff_week=15)
    assert [game.team_ids for game in games] == [
        (seeds[0], seeds[7]), (seeds[3], seeds[4]), (seeds[1], seeds[6]), (seeds[2], seeds[5])
    ]
    rankings = build_power_rankings(
        (MFLFantasyGame(1, tuple(seeds), tuple(120 - index for index in range(8))),),
        {team_id: team_id for team_id in seeds},
        current_week=2,
    )
    rounds = build_projected_playoff_rounds(
        seeds,
        {row.franchise_id: row for row in rankings},
        first_playoff_week=15,
    )
    assert [len(round_.games) for round_ in rounds] == [4, 2, 1]
    assert [round_.name for round_ in rounds] == ["Quarterfinals", "Semifinals", "Championship"]
