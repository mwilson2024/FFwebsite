from weekly_projections.mfl.client import MFLAvailability, MFLPlayer
from weekly_projections.recommendations import rank_available_players


def test_recommendations_rank_roster_upgrades_first() -> None:
    roster = [
        MFLPlayer("r1", "Roster Starter", "WR", "BUF"),
        MFLPlayer("r2", "Roster Bench", "WR", "NYJ"),
    ]
    available = [
        MFLPlayer("a1", "Clear Upgrade", "WR", "MIA"),
        MFLPlayer("a2", "Depth Player", "WR", "NEP"),
    ]
    states = {
        "a1": MFLAvailability("a1"),
        "a2": MFLAvailability("a2"),
    }
    board = rank_available_players(
        available_players=available,
        availability=states,
        roster=roster,
        projections={"r1": 16.0, "r2": 8.0, "a1": 13.0, "a2": 7.5},
    )

    assert board[0].player.id == "a1"
    assert board[0].roster_delta == 5.0
    assert board[0].suggested_drop.id == "r2"
    assert board[0].recommendation == "Strong target"


def test_locked_upgrade_is_kept_on_board_as_future_target() -> None:
    board = rank_available_players(
        available_players=[MFLPlayer("a1", "Locked Upgrade", "RB", "DAL")],
        availability={"a1": MFLAvailability("a1", status="locked", locked=True)},
        roster=[MFLPlayer("r1", "Roster Bench", "RB", "NYG")],
        projections={"a1": 12.0, "r1": 8.0},
    )
    assert board[0].recommendation == "Target when open"
    assert board[0].availability.claimable is False
