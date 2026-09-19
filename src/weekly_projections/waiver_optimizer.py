from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Iterable, Mapping

from weekly_projections.mfl.client import MFLPlayer, MFLTransaction
from weekly_projections.recommendations import PlayerRecommendation


@dataclass(frozen=True)
class WaiverQueueItem:
    priority: int
    add: MFLPlayer
    drop: MFLPlayer
    projection: float | None
    gain: float
    suggested_bid: int | None
    market_sample: int
    reason: str


@dataclass(frozen=True)
class WaiverQueue:
    items: tuple[WaiverQueueItem, ...]
    balance: int | None
    allocated: int | None
    history_sample: int


def _position(value: str) -> str:
    value = str(value or "").strip().upper().replace("D/ST", "DEF")
    return "DEF" if value == "DST" else "PK" if value == "K" else value


def optimize_waiver_queue(
    recommendations: Iterable[PlayerRecommendation],
    *,
    balance: float | None,
    transactions: Iterable[MFLTransaction],
    catalog: Mapping[str, MFLPlayer],
    locked_drop_ids: set[str] | frozenset[str] = frozenset(),
    max_claims: int = 6,
) -> WaiverQueue:
    """Build an advisory, budget-safe queue; it never submits MFL writes."""
    candidates = [
        item for item in recommendations
        if not item.is_rostered and item.is_claimable and item.suggested_drop is not None
        and item.suggested_drop.id not in locked_drop_ids
        and item.roster_delta is not None and item.roster_delta > 0
        and item.market_status in {"waiver", "locked"}
    ]
    candidates.sort(key=lambda item: (
        -(item.roster_delta or 0), -(item.projection or -999), item.player.name.casefold(),
    ))
    selected: list[PlayerRecommendation] = []
    position_counts: dict[str, int] = {}
    for item in candidates:
        position = _position(item.player.position)
        if position_counts.get(position, 0) >= 2:
            continue
        selected.append(item)
        position_counts[position] = position_counts.get(position, 0) + 1
        if len(selected) >= max_claims:
            break

    bids_by_position: dict[str, list[int]] = {}
    seen = set()
    for transaction in transactions:
        if transaction.id in seen or transaction.bid is None or transaction.bid < 0:
            continue
        seen.add(transaction.id)
        if "WAIVER" not in transaction.kind.upper() or len(transaction.adds) != 1:
            continue
        player = catalog.get(transaction.adds[0])
        if player:
            bids_by_position.setdefault(_position(player.position), []).append(transaction.bid)

    budget = (
        math.floor(balance)
        if balance is not None and math.isfinite(balance) and balance >= 0
        else None
    )
    remaining = budget
    queue = []
    for priority, item in enumerate(selected, 1):
        history = bids_by_position.get(_position(item.player.position), [])
        suggested: int | None = None
        if remaining is not None:
            if remaining <= 0:
                suggested = 0
            else:
                market = statistics.median(history) if history else max(1.0, budget * .04)
                gain_factor = max(.75, min(1.75, (item.roster_delta or 0) / 3))
                claim_cap = max(1, math.floor(budget * .22))
                suggested = min(remaining, claim_cap, max(1, round(market * gain_factor)))
                remaining -= suggested
        history_note = (
            f"{len(history)} same-position winning bid{'s' if len(history) != 1 else ''}"
            if history else "no same-position winning bids in the recent activity window"
        )
        queue.append(WaiverQueueItem(
            priority, item.player, item.suggested_drop,
            item.projection, float(item.roster_delta or 0), suggested, len(history),
            f"Projected +{item.roster_delta:.1f} over {item.suggested_drop.name}; {history_note}.",
        ))
    allocated = budget - remaining if budget is not None and remaining is not None else None
    return WaiverQueue(tuple(queue), budget, allocated, sum(len(values) for values in bids_by_position.values()))
