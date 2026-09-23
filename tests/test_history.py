from weekly_projections.history import build_historical_season
from weekly_projections.mfl.client import (
    MFLFantasyGame,
    MFLFranchise,
    MFLLeagueDetails,
)


def test_historical_season_normalizes_standings_and_matchups():
    details = MFLLeagueDetails(
        (("01", "East"),),
        {
            "0001": MFLFranchise("0001", "Alpha", "01"),
            "0002": MFLFranchise("0002", "Beta", "01"),
        },
        name="Archive League",
        start_week=1,
        end_week=17,
        last_regular_season_week=14,
    )
    archive = build_historical_season(
        season=2025,
        source_league_id="54321",
        details=details,
        standings=(
            {"id": "2", "h2hw": "10", "h2hl": "4", "h2ht": "0", "pf": "1600.25", "pa": "1400", "vp": "20.5"},
            {"id": "1", "h2hw": "8", "h2hl": "6", "h2ht": "0", "pf": "1500", "pa": "1450", "vp": "18"},
        ),
        schedule=(
            MFLFantasyGame(1, ("0001", "0002"), (101.25, 99.5)),
            MFLFantasyGame(2, ("0002", "0001"), (110.0, None)),
        ),
    )

    assert archive.season == 2025
    assert archive.league_name == "Archive League"
    assert archive.franchises[0].standing_rank == 2
    assert archive.franchises[1].standing_rank == 1
    assert archive.franchises[1].points_for == 1600.25
    assert [(row.week, row.matchup_index, row.franchise_id, row.score) for row in archive.matchup_teams] == [
        (1, 1, "0001", 101.25),
        (1, 1, "0002", 99.5),
        (2, 1, "0002", 110.0),
        (2, 1, "0001", None),
    ]
