from __future__ import annotations

from weekly_projections.mfl.client import MFLAvailability, MFLPlayer, MFLTransaction
from weekly_projections.recommendations import PlayerRecommendation
from weekly_projections.waiver_optimizer import optimize_waiver_queue


def row(player_id, position, projection, delta, drop, *, locked=True, status="waiver"):
    player = MFLPlayer(player_id, f"Add {player_id}", position, "DET")
    return PlayerRecommendation(
        player, MFLAvailability(player_id, status=status, locked=locked),
        projection, delta, drop, "Waiver target", "good", "Upgrade",
    )


def test_optimizer_orders_claims_uses_market_and_never_exceeds_faab():
    wr_drop = MFLPlayer("dw", "Drop WR", "WR", "BUF")
    rb_drop = MFLPlayer("dr", "Drop RB", "RB", "MIA")
    catalog = {
        "old-wr": MFLPlayer("old-wr", "Past WR", "WR", "KC"),
        "old-rb": MFLPlayer("old-rb", "Past RB", "RB", "LV"),
    }
    history = (
        MFLTransaction("1", "BBID_WAIVER", 1, ("0002",), ("old-wr",), (), bid=5),
        MFLTransaction("2", "BBID_WAIVER", 1, ("0003",), ("old-rb",), (), bid=3),
    )
    queue = optimize_waiver_queue(
        [row("wr1", "WR", 18, 6, wr_drop), row("rb1", "RB", 15, 4, rb_drop),
         row("wr2", "WR", 14, 2, wr_drop)],
        balance=7, transactions=history, catalog=catalog,
    )
    assert [item.add.id for item in queue.items][:2] == ["wr1", "rb1"]
    assert queue.allocated == sum(item.suggested_bid or 0 for item in queue.items)
    assert queue.allocated <= queue.balance == 7
    assert [item.priority for item in queue.items] == list(range(1, len(queue.items) + 1))
    assert queue.history_sample == 2


def test_optimizer_excludes_open_players_non_upgrades_and_locked_drops():
    locked_drop = MFLPlayer("locked", "Locked Drop", "WR", "BUF")
    safe_drop = MFLPlayer("safe", "Safe Drop", "RB", "MIA")
    open_player = row("open", "RB", 20, 8, safe_drop, locked=False, status="available")
    negative = row("negative", "RB", 5, -1, safe_drop)
    locked = row("claim", "WR", 15, 4, locked_drop)
    queue = optimize_waiver_queue(
        [open_player, negative, locked], balance=10, transactions=(), catalog={},
        locked_drop_ids={"locked"},
    )
    assert queue.items == ()
