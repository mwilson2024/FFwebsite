"""Read-only weekly streamer/waiver snapshots, independent of browser visits."""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timedelta
from threading import RLock
from zoneinfo import ZoneInfo
import time

from weekly_projections.defense_streaming import DefenseBid, DefenseStream, _team_code, defense_waiver_pricing, rank_defense_streams
from weekly_projections.defense_talent import load_defense_talent
from weekly_projections.mfl.client import MFLAvailability, MFLPlayer, MFLTransaction
from weekly_projections.recommendations import PlayerRecommendation
from weekly_projections.waiver_optimizer import WaiverQueue, WaiverQueueItem, optimize_waiver_queue

EASTERN = ZoneInfo("America/New_York")
REPORT = "weekly-advisory"
VERSION = 1


def weekly_cycle(now: float) -> datetime:
    local = datetime.fromtimestamp(now, EASTERN)
    due = (local - timedelta(days=(local.weekday() - 1) % 7)).replace(
        hour=2, minute=0, second=0, microsecond=0,
    )
    if local < due:
        due -= timedelta(days=7)
    return due


def _board(inputs: dict) -> list[PlayerRecommendation]:
    result = []
    if len(inputs["board"]) > 5000:
        raise ValueError("Weekly input exceeds player bounds")
    for raw in inputs["board"]:
        row = dict(raw)
        row["player"] = MFLPlayer.from_dict(row["player"])
        row["availability"] = MFLAvailability(**row["availability"])
        row["suggested_drop"] = MFLPlayer.from_dict(row["suggested_drop"]) if row["suggested_drop"] else None
        result.append(PlayerRecommendation(**row))
    return result


def compute_plan(inputs: dict, *, year: int, week: int, now: float, talent: dict) -> dict:
    board = _board(inputs)
    catalog = {key: MFLPlayer.from_dict(value) for key, value in inputs["catalog"].items()}
    transactions = tuple(MFLTransaction(**row) for row in inputs["activity"])
    # Reapply the window on the scheduled day, not just when inputs were saved.
    transactions = tuple(t for t in transactions if t.timestamp is not None
                         and now - 14 * 86400 <= t.timestamp <= now)
    streams = rank_defense_streams(board, year=year, week=week, talent=talent,
                                   games=inputs["games"], opponent_strength=inputs["strength"], now=now)
    pricing = defense_waiver_pricing(
        [s for s in streams if not s.item.is_rostered and s.item.market_status in {"waiver", "locked"}],
        balance=inputs["balance"], transactions=transactions, catalog=catalog, now=now,
    )
    queue = optimize_waiver_queue(board, balance=inputs["balance"], transactions=transactions,
                                 catalog=catalog, locked_drop_ids=set(inputs["locked"]))
    return {
        "streams": [{**{k: v for k, v in asdict(s).items() if k != "item"},
                     "player_id": s.item.player.id} for s in streams],
        "pricing": {**pricing, "bids": {key: asdict(bid) for key, bid in pricing["bids"].items()}},
        "queue": asdict(queue), "pricing_error": inputs.get("pricing_error"),
    }


def plan_context(snapshot: dict, board: list[PlayerRecommendation], locked: set[str], *, now: float,
                 roster: list[MFLPlayer] | tuple = (), games: dict | None = None) -> dict:
    """Recheck display eligibility without rerunning optimization or any MFL call."""
    items = {item.player.id: item for item in board}
    owned = {player.id: player for player in roster}
    owned.update({item.player.id: item.player for item in board if item.market_status == "mine"})
    schedule = {_team_code(team): value for team, value in (games or {}).items()}
    streams = []
    for raw in snapshot["plan"]["streams"]:
        row = dict(raw)
        item = items.get(row.pop("player_id"))
        if not item or (item.is_rostered and item.market_status != "mine") or (
            not item.is_rostered and not item.is_claimable
        ):
            continue
        stream = DefenseStream(item=item, **row)
        game = schedule.get(_team_code(item.player.team), {})
        if game.get("final") or 0 < float(game.get("kickoff") or 0) <= now:
            stream = replace(stream, score=None, label="Game already started",
                             explanation="This saved fit is no longer a current-week stream. Any claim is for a later waiver run.")
        streams.append(stream)
    queue = snapshot["plan"]["queue"]
    claims = []
    for raw in queue["items"]:
        add, drop = items.get(raw["add"]["id"]), owned.get(raw["drop"]["id"])
        if not add or not drop or add.is_rostered or not add.is_claimable or drop.id in locked \
                or add.market_status not in {"waiver", "locked"}:
            continue
        row = {**raw, "add": add.player, "drop": drop, "priority": len(claims) + 1}
        claims.append(WaiverQueueItem(**row))
    pricing = dict(snapshot["plan"]["pricing"])
    pricing["bids"] = {key: DefenseBid(**value) for key, value in pricing["bids"].items() if key in items}
    computed = datetime.fromtimestamp(snapshot["computed_at"], EASTERN)
    captured = datetime.fromtimestamp(snapshot["inputs_at"], EASTERN)
    next_run = weekly_cycle(now) + timedelta(days=7)
    return {
        "defense_targets": [s for s in streams if not s.item.is_rostered][:6],
        "owned_defenses": [s for s in streams if s.item.market_status == "mine"],
        "defense_pricing": pricing,
        "defense_pricing_error": snapshot["plan"].get("pricing_error"),
        "waiver_queue": WaiverQueue(tuple(claims), queue["balance"],
                                    sum(c.suggested_bid or 0 for c in claims) if queue["balance"] is not None else None,
                                    queue["history_sample"]),
        "weekly_plan": {
            "computed": computed.strftime("%b %d, %I:%M %p %Z"),
            "inputs": captured.strftime("%b %d, %I:%M %p %Z"),
            "next": next_run.strftime("%b %d, %I:%M %p %Z"),
            "stale_inputs": now - snapshot["inputs_at"] > 86400,
            "budget": datetime.fromtimestamp(snapshot["inputs"].get("budget_at", snapshot["inputs_at"]), EASTERN).strftime("%b %d, %I:%M %p %Z"),
        },
    }


class WeeklyAdvisoryCache:
    """Single-worker cache; database stores only bounded, display-only inputs.

    Initial setup/new-week catch-up runs once. Normal visits reuse the frozen
    result but capture the latest board for the next Tuesday. The timer can
    compute from saved inputs even after the MFL browser session has expired.
    It never authenticates, submits claims, or restores a pending transaction.
    """

    def __init__(self, store, log_error):
        self.store = store
        self.log_error = log_error
        self.lock = RLock()
        self.memory: dict[tuple, dict] = {}
        self.talent: dict[tuple, dict] = {}

    def _talent(self, year: int, week: int, now: float) -> dict:
        if week <= 3:
            return {}
        key = (year, week, weekly_cycle(now).date().isoformat())
        if key not in self.talent:
            store = self.store()
            stored = None
            try:
                if store:
                    stored = store.load_league_report_snapshot(
                        "public-defense-talent-v1", year=year, league_id="0",
                        report_type="defense-talent", report_key=f"{week}:{key[2]}",
                    )
            except Exception as error:
                self.log_error("defense_talent_cache_load_failed", error)
            try:
                value = stored["payload"] if stored else load_defense_talent(year, week)
            except Exception as error:
                self.log_error("defense_talent_unavailable", error)
                value = {}
            if store and not stored:
                try:
                    store.save_league_report_snapshot(
                        "public-defense-talent-v1", year=year, league_id="0",
                        report_type="defense-talent", report_key=f"{week}:{key[2]}",
                        payload=value, ttl_seconds=8 * 86400,
                    )
                except Exception as error:
                    self.log_error("defense_talent_cache_save_failed", error)
            if len(self.talent) >= 64:
                self.talent.pop(next(iter(self.talent)))
            self.talent[key] = value
        return self.talent[key]

    def resolve(self, owner: str, year: int, league: str, week: int, *, inputs: dict,
                prepare, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        key = (owner, year, league)
        with self.lock:
            stored = self.memory.get(key)
            store = self.store()
            if stored is None and store:
                try:
                    record = store.load_league_report_snapshot(owner, year=year, league_id=league, report_type=REPORT)
                    stored = record["payload"] if record else None
                except Exception as error:
                    self.log_error("weekly_advisory_load_failed", error)
            if stored and (stored.get("version") != VERSION or stored.get("week") != week):
                stored = None
            cycle = weekly_cycle(now).date().isoformat()
            due = not stored or stored.get("cycle") != cycle
            # Budget/history provider reads occur only when creating a plan.
            if due:
                inputs = {**inputs, **prepare()}
            else:
                inputs = {**stored["inputs"], **inputs}
            snapshot = {
                "version": VERSION, "week": week, "cycle": cycle,
                "inputs_at": now, "inputs": inputs,
                "computed_at": now if due else stored["computed_at"],
                "plan": compute_plan(inputs, year=year, week=week, now=now,
                                     talent=self._talent(year, week, now)) if due else stored["plan"],
            }
            # Keep the input timestamp that actually produced the frozen plan.
            snapshot["plan_inputs_at"] = now if due else stored.get("plan_inputs_at", stored["inputs_at"])
            if len(self.memory) >= 256 and key not in self.memory:
                self.memory.pop(next(iter(self.memory)))
            self.memory[key] = snapshot
            if store:
                try:
                    store.save_league_report_snapshot(owner, year=year, league_id=league,
                                                     report_type=REPORT, payload=snapshot,
                                                     ttl_seconds=8 * 86400)
                except Exception as error:
                    self.log_error("weekly_advisory_save_failed", error)
            return {**snapshot, "inputs_at": snapshot["plan_inputs_at"]}

    def registered(self, owner: str, year: int, league: str) -> bool:
        """Only refresh MFL for leagues where the owner has used weekly plans."""
        with self.lock:
            if (owner, year, league) in self.memory:
                return True
            store = self.store()
            return bool(store and store.load_league_report_snapshot(
                owner, year=year, league_id=league, report_type=REPORT,
            ))

    def run_due(self, *, now: float | None = None) -> int:
        """One bounded Tuesday/catch-up pass over persisted read-only inputs."""
        now = time.time() if now is None else now
        cycle = weekly_cycle(now).date().isoformat()
        count = 0
        with self.lock:
            store = self.store()
            if not store:
                return 0
            for task in store.load_weekly_advisory_tasks(cycle=cycle, limit=100):
                snapshot = dict(task["payload"])
                if snapshot.get("version") != VERSION or not 1 <= snapshot.get("week", 0) <= 18:
                    continue
                if now - snapshot.get("inputs_at", 0) > 8 * 86400:
                    continue
                try:
                    plan = compute_plan(snapshot["inputs"], year=task["year"], week=snapshot["week"],
                                        now=now, talent=self._talent(task["year"], snapshot["week"], now))
                    updated = {**snapshot, "cycle": cycle, "computed_at": now,
                               "plan_inputs_at": snapshot["inputs_at"], "plan": plan}
                    updated.pop("attempt_cycle", None)
                    if store.finish_weekly_advisory_task(task, updated):
                        count += 1
                        # Next visit reloads its persisted result.
                        self.memory.clear()
                except Exception as error:
                    self.log_error("weekly_advisory_compute_failed", error)
                    store.finish_weekly_advisory_task(task, {**snapshot, "attempt_cycle": cycle})
        return count
