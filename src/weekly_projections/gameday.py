"""Observed matchup changes for the week-long GameDay timeline.

MFL's liveScoring export reports fantasy totals, not NFL play descriptions.  This
module therefore records only changes the application actually observes and never
invents a play, yardage total, lineup submission time, or scoring category.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable


def observe_scoring(
    state: dict[str, Any] | None,
    teams: Iterable[dict[str, Any]],
    win_percentages: tuple[float | None, float | None] | None,
    *,
    observed_at: float,
    timezone,
    record_events: bool = True,
    tracked_lineup_team_id: str = "",
) -> dict[str, Any]:
    """Return bounded odds and observed scoring/lineup history."""
    state = dict(state or {})
    previous = state.get("snapshot") if isinstance(state.get("snapshot"), dict) else None
    normalized = []
    for team in teams:
        normalized.append({
            "id": str(team["id"]),
            "name": str(team["name"]),
            "score": round(float(team["score"]), 2),
            "players": {
                str(player["id"]): {
                    "name": str(player["name"]),
                    "score": round(float(player["score"]), 2),
                    "starter": bool(player.get("starter", False)),
                }
                for player in team.get("players", ())
            },
        })
    try:
        stamp = datetime.fromtimestamp(observed_at, timezone).strftime("%a %-I:%M:%S %p")
    except ValueError:  # Windows strftime does not support %-I.
        stamp = datetime.fromtimestamp(observed_at, timezone).strftime("%a %I:%M:%S %p").replace(" 0", " ")
    events = list(state.get("events") or [])
    if record_events and previous is None and normalized:
        events.append({
            "observed_at": observed_at,
            "time": stamp,
            "title": "Live scoring baseline",
            "detail": " · ".join(f"{team['name']} {team['score']:.2f}" for team in normalized),
            "tone": "baseline",
        })
    elif record_events and previous:
        old_teams = {team["id"]: team for team in previous.get("teams", ())}
        for team in normalized:
            old_team = old_teams.get(team["id"], {"score": 0.0, "players": {}})
            old_players = old_team.get("players", {})
            player_changes = []
            for player_id, player in team["players"].items():
                old_score = float(old_players.get(player_id, {}).get("score", 0.0))
                delta = round(player["score"] - old_score, 2)
                if delta:
                    player_changes.append((abs(delta), player, old_score, delta))
            for _, player, old_score, delta in sorted(player_changes, reverse=True):
                events.append({
                    "observed_at": observed_at,
                    "time": stamp,
                    "title": f"{player['name']} {delta:+.2f} fantasy points",
                    "detail": (
                        f"{team['name']} · MFL player total {old_score:.2f} → {player['score']:.2f}"
                        + (" · starter" if player["starter"] else " · bench")
                    ),
                    "tone": "gain" if delta > 0 else "loss",
                })
            team_delta = round(team["score"] - float(old_team.get("score", 0.0)), 2)
            if team_delta and not player_changes:
                events.append({
                    "observed_at": observed_at,
                    "time": stamp,
                    "title": f"{team['name']} {team_delta:+.2f} team points",
                    "detail": (
                        f"Official MFL team total {float(old_team.get('score', 0.0)):.2f} → {team['score']:.2f}; "
                        "the player-level change was not present in this snapshot."
                    ),
                    "tone": "gain" if team_delta > 0 else "loss",
                })
    tracked_team = next(
        (team for team in normalized if team["id"].zfill(4) == str(tracked_lineup_team_id).zfill(4)),
        None,
    ) if tracked_lineup_team_id else None
    current_starters = {
        player_id: player["name"]
        for player_id, player in (tracked_team or {}).get("players", {}).items()
        if player["starter"]
    }
    previous_lineup = state.get("lineup_snapshot")
    if tracked_team and current_starters:
        lineup_changed = False
        if not isinstance(previous_lineup, dict) \
                or previous_lineup.get("team_id", "").zfill(4) != tracked_team["id"].zfill(4):
            events.append({
                "observed_at": observed_at,
                "time": stamp,
                "title": f"{tracked_team['name']} initial lineup observed",
                "detail": f"{len(current_starters)} starters · " + ", ".join(current_starters.values()),
                "tone": "lineup-baseline",
                "kind": "lineup",
            })
            lineup_changed = True
        else:
            old_starters = previous_lineup.get("starters")
            old_starters = old_starters if isinstance(old_starters, dict) else {}
            started = [current_starters[player_id] for player_id in current_starters.keys() - old_starters.keys()]
            benched = [old_starters[player_id] for player_id in old_starters.keys() - current_starters.keys()]
            if started or benched:
                details = []
                if started:
                    details.append("Started " + ", ".join(sorted(started)))
                if benched:
                    details.append("Benched " + ", ".join(sorted(benched)))
                events.append({
                    "observed_at": observed_at,
                    "time": stamp,
                    "title": f"{tracked_team['name']} changed the lineup",
                    "detail": " · ".join(details) + " · change time is when Fantasy HQ observed it",
                    "tone": "lineup-change",
                    "kind": "lineup",
                })
                lineup_changed = True
        if lineup_changed:
            state["lineup_snapshot"] = {
                "team_id": tracked_team["id"], "team_name": tracked_team["name"],
                "starters": current_starters, "observed_at": observed_at,
            }
    probability = list(state.get("probability") or [])
    if win_percentages and all(value is not None for value in win_percentages):
        point = {
            "observed_at": observed_at,
            "time": stamp,
            "left": round(float(win_percentages[0]), 2),
            "right": round(float(win_percentages[1]), 2),
        }
        if not probability or probability[-1]["left"] != point["left"] \
                or probability[-1]["right"] != point["right"]:
            probability.append(point)
    state["snapshot"] = {"teams": normalized, "observed_at": observed_at}
    state["events"] = events[-200:]
    state["probability"] = probability[-500:]
    return state


def chart_points(
    points: list[dict[str, Any]], *, field: str = "left", width: int = 720, height: int = 180,
) -> str:
    """Create time-scaled SVG points for either team's estimated win chance."""
    if not points:
        return ""
    if field not in {"left", "right"}:
        raise ValueError("Choose the left or right win-probability series")
    observed = [float(point.get("observed_at", index)) for index, point in enumerate(points)]
    if len(points) == 1 or observed[-1] <= observed[0]:
        xs = [width / 2]
    else:
        elapsed = observed[-1] - observed[0]
        xs = [(timestamp - observed[0]) * width / elapsed for timestamp in observed]
    ys = [height - max(0.0, min(100.0, float(point[field]))) * height / 100 for point in points]
    return " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
