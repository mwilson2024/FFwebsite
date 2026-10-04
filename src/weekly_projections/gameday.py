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
    tracked_lineup_team_ids: Iterable[str] = (),
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
            for _, player, old_score, delta in sorted(
                player_changes,
                key=lambda change: (
                    -change[0],
                    str(change[1].get("name") or "").casefold(),
                    -change[3],
                ),
            ):
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
    tracked_ids = {str(team_id).zfill(4) for team_id in tracked_lineup_team_ids if team_id}
    if tracked_lineup_team_id:
        tracked_ids.add(str(tracked_lineup_team_id).zfill(4))
    lineup_history = dict(state.get("lineup_history") or {})
    legacy_lineup = state.get("lineup_snapshot")
    if isinstance(legacy_lineup, dict) and legacy_lineup.get("team_id"):
        legacy_id = str(legacy_lineup["team_id"]).zfill(4)
        lineup_history.setdefault(legacy_id, {
            "team_id": legacy_id,
            "team_name": str(legacy_lineup.get("team_name") or "Team"),
            "initial_starters": dict(legacy_lineup.get("starters") or {}),
            "current_starters": dict(legacy_lineup.get("starters") or {}),
            "initial_observed_at": float(legacy_lineup.get("observed_at") or observed_at),
            "observed_at": float(legacy_lineup.get("observed_at") or observed_at),
            "changes": 0,
        })
    for tracked_team in normalized:
        team_id = tracked_team["id"].zfill(4)
        if team_id not in tracked_ids:
            continue
        current_starters = {
            player_id: player["name"]
            for player_id, player in tracked_team["players"].items()
            if player["starter"]
        }
        if not current_starters:
            continue
        history = lineup_history.get(team_id)
        lineup_changed = False
        if not isinstance(history, dict):
            history = {
                "team_id": team_id, "team_name": tracked_team["name"],
                "initial_starters": dict(current_starters),
                "current_starters": dict(current_starters),
                "initial_observed_at": observed_at, "observed_at": observed_at,
                "changes": 0,
            }
            events.append({
                "observed_at": observed_at, "time": stamp, "team_id": team_id,
                "title": f"{tracked_team['name']} initial lineup observed",
                "detail": f"{len(current_starters)} starters · " + ", ".join(current_starters.values()),
                "tone": "lineup-baseline", "kind": "lineup",
            })
            lineup_changed = True
        else:
            old_starters = history.get("current_starters")
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
                    "observed_at": observed_at, "time": stamp, "team_id": team_id,
                    "title": f"{tracked_team['name']} changed the lineup",
                    "detail": " · ".join(details) + " · change time is when Fantasy HQ observed it",
                    "tone": "lineup-change", "kind": "lineup",
                })
                history["changes"] = int(history.get("changes") or 0) + 1
                lineup_changed = True
            history.update(team_name=tracked_team["name"], current_starters=dict(current_starters))
            if lineup_changed:
                history["observed_at"] = observed_at
        lineup_history[team_id] = history
        if lineup_changed and tracked_lineup_team_id \
                and team_id == str(tracked_lineup_team_id).zfill(4):
            state["lineup_snapshot"] = {
                "team_id": team_id, "team_name": tracked_team["name"],
                "starters": current_starters, "observed_at": observed_at,
            }
    if lineup_history:
        state["lineup_history"] = lineup_history
    probability = list(state.get("probability") or [])
    if win_percentages and all(value is not None for value in win_percentages):
        point = {
            "observed_at": observed_at,
            "time": stamp,
            "left": round(float(win_percentages[0]), 2),
            "right": round(float(win_percentages[1]), 2),
        }
        last_observation = float(probability[-1].get("observed_at") or 0) if probability else 0
        if not probability or probability[-1]["left"] != point["left"] \
                or probability[-1]["right"] != point["right"] \
                or observed_at - last_observation >= 30 * 60:
            probability.append(point)
    state["snapshot"] = {"teams": normalized, "observed_at": observed_at}
    state["events"] = events[-200:]
    state["probability"] = probability[-500:]
    return state


def lineup_what_if(
    state: dict[str, Any] | None,
    team_id: str,
    *,
    week: int | None = None,
    final: bool = False,
) -> dict[str, Any] | None:
    """Grade observed starter changes against the first lineup Fantasy HQ saw."""
    if not isinstance(state, dict):
        return None
    normalized_id = str(team_id).zfill(4)
    histories = state.get("lineup_history")
    history = histories.get(normalized_id) if isinstance(histories, dict) else None
    snapshot = state.get("snapshot")
    teams = snapshot.get("teams") if isinstance(snapshot, dict) else None
    team = next(
        (item for item in teams if isinstance(item, dict)
         and str(item.get("id") or "").zfill(4) == normalized_id),
        None,
    ) if isinstance(teams, list) else None
    if not isinstance(history, dict) or not isinstance(team, dict) \
            or int(history.get("changes") or 0) < 1:
        return None
    initial = history.get("initial_starters")
    current = history.get("current_starters")
    players = team.get("players")
    if not all(isinstance(item, dict) for item in (initial, current, players)):
        return None
    if not initial or not current or not set(initial).issubset(players) or not set(current).issubset(players):
        return None
    initial_total = round(sum(float(players[player_id]["score"]) for player_id in initial), 2)
    current_total = round(sum(float(players[player_id]["score"]) for player_id in current), 2)
    delta = round(current_total - initial_total, 2)
    started = [current[player_id] for player_id in current.keys() - initial.keys()]
    benched = [initial[player_id] for player_id in initial.keys() - current.keys()]
    if delta > 0.005:
        verdict, tone = (("The lineup change was right" if final else "The change is helping so far"), "positive")
    elif delta < -0.005:
        verdict, tone = (("Keeping the initial lineup would have scored more" if final else "The initial lineup is ahead so far"), "negative")
    else:
        verdict, tone = (("The change finished even" if final else "No scoring difference yet"), "neutral")
    return {
        "team_id": normalized_id,
        "team_name": str(history.get("team_name") or team.get("name") or "Team"),
        "week": week,
        "initial_total": initial_total,
        "current_total": current_total,
        "delta": delta,
        "verdict": verdict,
        "tone": tone,
        "started": tuple(sorted(started)),
        "benched": tuple(sorted(benched)),
        "changes": int(history.get("changes") or 0),
        "final": bool(final),
        "official_total": round(float(team.get("score") or 0), 2),
    }


def chart_points(
    points: list[dict[str, Any]], *, field: str = "left", width: int = 720, height: int = 180,
) -> str:
    """Create time-scaled SVG points for either team's estimated win chance."""
    if not points:
        return ""
    if field not in {"left", "right"}:
        raise ValueError("Choose the left or right win-probability series")
    observed = [float(point.get("observed_at", index)) for index, point in enumerate(points)]
    if len(points) == 1:
        value = height - max(0.0, min(100.0, float(points[0][field]))) * height / 100
        return f"0.0,{value:.1f} {float(width):.1f},{value:.1f}"
    if observed[-1] <= observed[0]:
        xs = [index * width / (len(points) - 1) for index in range(len(points))]
    else:
        elapsed = observed[-1] - observed[0]
        xs = [(timestamp - observed[0]) * width / elapsed for timestamp in observed]
    ys = [height - max(0.0, min(100.0, float(point[field]))) * height / 100 for point in points]
    return " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
