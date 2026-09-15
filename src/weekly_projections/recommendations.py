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
    fantasy_team_id: str = ""
    fantasy_team_name: str = ""
    bye_replacements: tuple[str, ...] = ()

    @property
    def is_rostered(self) -> bool:
        return bool(self.fantasy_team_id)

    @property
    def is_claimable(self) -> bool:
        # A kickoff lock prevents an immediate FCFS add, but MFL can still
        # accept a priority/FAAB claim for processing in the next waiver run.
        return not self.is_rostered and self.availability.waiver_claimable

    @property
    def market_status(self) -> str:
        if self.is_rostered:
            return "mine" if self.availability.status == "mine" else "rostered"
        if self.availability.locked:
            return "locked"
        if self.availability.status.casefold() in {"waiver", "waivers"}:
            return "waiver"
        return "open"

def _recommendation_copy(
    *, projection: float | None, delta: float | None, locked: bool
) -> tuple[str, str, str]:
    if projection is None:
        return (
            "Waiver watch" if locked else "Needs projection",
            "muted",
            "MFL has not published a projection for this player yet."
            + (" Immediate FCFS is locked, but a waiver claim can still be filed." if locked else ""),
        )
    if delta is None:
        return (
            "Waiver watch" if locked else "Review fit",
            "muted",
            "There is no projected roster player at the same position to compare."
            + (" Immediate FCFS is locked, but a waiver claim can still be filed." if locked else ""),
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
        label = "Waiver target"
    elif locked:
        label = "Waiver watch"
    reason = (
        f"Projects {abs(delta):.1f} points {'above' if delta >= 0 else 'below'} "
        "your lowest projected player at this position."
    )
    if locked:
        reason += " Immediate FCFS is locked, but a waiver claim can still be filed."
    return label, tone, reason


def rank_available_players(
    *,
    available_players: Iterable[MFLPlayer],
    availability: Mapping[str, MFLAvailability],
    roster: Iterable[MFLPlayer],
    projections: Mapping[str, float],
    bye_teams: set[str] | frozenset[str] = frozenset(),
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
        bye_peers = tuple(
            peer.name for peer in roster_by_position.get(player.position.casefold(), [])
            if peer.team.upper() in bye_teams
        )
        if bye_peers:
            reason += f" Covers {', '.join(bye_peers)} during this week's bye."
            if not state.locked and label in {"Review fit", "Depth only", "Small edge"}:
                label, tone = "Bye-week fit", "good"
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
                bye_replacements=bye_peers,
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


def build_player_board(
    *,
    available_players: Iterable[MFLPlayer],
    availability: Mapping[str, MFLAvailability],
    rostered_players: Iterable[MFLPlayer],
    rostered_by: Mapping[str, str],
    franchise_names: Mapping[str, str],
    own_franchise_id: str,
    own_roster: Iterable[MFLPlayer],
    projections: Mapping[str, float],
    bye_teams: set[str] | frozenset[str] = frozenset(),
) -> list[PlayerRecommendation]:
    """Combine free agents and rostered players into one searchable league board."""
    own_roster = tuple(own_roster)
    board = rank_available_players(
        available_players=available_players,
        availability=availability,
        roster=own_roster,
        projections=projections,
        bye_teams=bye_teams,
    )
    own_by_position: dict[str, list[MFLPlayer]] = {}
    for player in own_roster:
        own_by_position.setdefault(player.position.casefold(), []).append(player)

    own_id = own_franchise_id.zfill(4)
    for player in rostered_players:
        franchise_id = str(rostered_by.get(player.id, "")).zfill(4)
        if not franchise_id or franchise_id == "0000":
            continue
        franchise_name = franchise_names.get(franchise_id, f"Team {franchise_id}")
        projection = projections.get(player.id)
        peers = [
            peer
            for peer in own_by_position.get(player.position.casefold(), [])
            if peer.id in projections and peer.id != player.id
        ]
        suggested_drop = min(peers, key=lambda peer: projections[peer.id]) if peers else None
        delta = (
            projection - projections[suggested_drop.id]
            if projection is not None and suggested_drop is not None
            else None
        )
        is_mine = franchise_id == own_id
        if is_mine:
            label, tone = "Your roster", "muted"
            reason = f"Currently rostered by {franchise_name}."
        else:
            label = "Trade target" if delta is not None and delta >= 0.5 else "Rostered"
            tone = "good" if label == "Trade target" else "muted"
            comparison = (
                f" Projects {abs(delta):.1f} points {'above' if delta >= 0 else 'below'} "
                f"{suggested_drop.name}."
                if delta is not None and suggested_drop is not None
                else ""
            )
            reason = f"Rostered by {franchise_name}.{comparison} Use Trade Center to build an offer."
        board.append(
            PlayerRecommendation(
                player=player,
                availability=MFLAvailability(
                    player_id=player.id,
                    status="mine" if is_mine else "rostered",
                    locked=False,
                ),
                projection=projection,
                roster_delta=delta,
                suggested_drop=suggested_drop,
                recommendation=label,
                recommendation_tone=tone,
                reason=reason,
                fantasy_team_id=franchise_id,
                fantasy_team_name=franchise_name,
            )
        )

    status_order = {"open": 0, "waiver": 1, "locked": 2, "rostered": 3, "mine": 4}
    return sorted(
        board,
        key=lambda item: (
            status_order.get(item.market_status, 9),
            -(item.roster_delta if item.roster_delta is not None else -999.0),
            -(item.projection if item.projection is not None else -999.0),
            item.player.name.casefold(),
        ),
    )
