from weekly_projections.mfl.client import MFLAvailability, MFLPlayer
from weekly_projections.recommendations import build_player_board, rank_available_players


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


def test_available_player_is_labeled_as_bye_week_fit() -> None:
    board = rank_available_players(
        available_players=[MFLPlayer("a1", "Bye Fill", "WR", "DET")],
        availability={"a1": MFLAvailability("a1")},
        roster=[MFLPlayer("r1", "Starter On Bye", "WR", "BUF")],
        projections={"a1": 8.0, "r1": 10.0},
        bye_teams={"BUF"},
    )
    assert board[0].recommendation == "Bye-week fit"
    assert board[0].bye_replacements == ("Starter On Bye",)
    assert "during this week's bye" in board[0].reason


def test_full_player_board_labels_rostered_teams_without_making_them_addable() -> None:
    own = MFLPlayer("mine", "My Receiver", "WR", "DET")
    free_agent = MFLPlayer("free", "Free Receiver", "WR", "GB")
    rostered = MFLPlayer("other", "Rival Receiver", "WR", "MIN")
    board = build_player_board(
        available_players=[free_agent],
        availability={"free": MFLAvailability("free")},
        rostered_players=[own, rostered],
        rostered_by={"mine": "0001", "other": "0002"},
        franchise_names={"0001": "My Team", "0002": "Rival Team"},
        own_franchise_id="0001",
        own_roster=[own],
        projections={"mine": 8.0, "free": 12.0, "other": 14.0},
    )
    rows = {item.player.id: item for item in board}
    assert rows["free"].is_claimable is True
    assert rows["free"].market_status == "open"
    assert rows["mine"].market_status == "mine"
    assert rows["other"].market_status == "rostered"
    assert rows["other"].fantasy_team_name == "Rival Team"
    assert rows["other"].is_claimable is False
