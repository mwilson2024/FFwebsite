from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from weekly_projections.mfl.client import MFLAvailability, MFLPlayer


@dataclass(frozen=True)
class PlayerRecommendation:
    player: MFLPlayer
    availability: MFLAvailability
    projection: float | None
    roster_delta: float | None
    suggested_drop: MFLPlayer | None
    recommendation: str
    recommendation_tone: str
    reason: str

def _recommendation_copy(
    *, projection: float | None, delta: float | None, locked: bool
) -> tuple[str, str, str]:
    if projection is None:
        return (
            "Watch list" if locked else "Needs projection",
            "muted",
            "MFL has not published a projection for this player yet.",
        )
    if delta is None:
        return (
            "Watch list" if locked else "Review fit",
            "muted",
            "There is no projected roster player at the same position to compare.",
        )
    if delta >= 4:
        label, tone = "Strong target", "strong"
    elif delta >= 2:
        label, tone = "Upgrade", "good"
    elif delta >= 0.5:
        label, tone = "Small edge", "fair"
    else:
        label, tone = "Depth only", "muted"
    if locked and delta >= 0.5:
        label = "Target when open"
    elif locked:
        label = "Watch list"
    reason = (
        f"Projects {abs(delta):.1f} points {'above' if delta >= 0 else 'below'} "
        "your lowest projected player at this position."
    )
    return label, tone, reason


def rank_available_players(
    *,
    available_players: Iterable[MFLPlayer],
    availability: Mapping[str, MFLAvailability],
    roster: Iterable[MFLPlayer],
    projections: Mapping[str, float],
) -> list[PlayerRecommendation]:
    """Rank the whole available pool against the weakest projected roster peer."""
    roster_by_position: dict[str, list[MFLPlayer]] = {}
    for player in roster:
        roster_by_position.setdefault(player.position.casefold(), []).append(player)

    recommendations: list[PlayerRecommendation] = []
    for player in available_players:
        peers = [
            peer
            for peer in roster_by_position.get(player.position.casefold(), [])
            if peer.id in projections
        ]
        suggested_drop = min(peers, key=lambda peer: projections[peer.id]) if peers else None
        projection = projections.get(player.id)
        delta = (
            projection - projections[suggested_drop.id]
            if projection is not None and suggested_drop is not None
            else None
        )
        state = availability.get(player.id, MFLAvailability(player_id=player.id))
        label, tone, reason = _recommendation_copy(
            projection=projection,
            delta=delta,
            locked=state.locked,
        )
        recommendations.append(
            PlayerRecommendation(
                player=player,
                availability=state,
                projection=projection,
                roster_delta=delta,
                suggested_drop=suggested_drop,
                recommendation=label,
                recommendation_tone=tone,
                reason=reason,
            )
        )
    return sorted(
        recommendations,
        key=lambda item: (
            -(item.roster_delta if item.roster_delta is not None else -999.0),
            -(item.projection if item.projection is not None else -999.0),
            item.player.name.casefold(),
        ),
    )
