"""Bounded, explainable one-for-one ideas, not dynasty trade valuations."""
from dataclasses import dataclass
import math
import time
from itertools import combinations

from weekly_projections.lineup import _solve_lineup
from weekly_projections.mfl.client import MFLPlayer


@dataclass(frozen=True)
class TradeIdea:
    target: str
    give: MFLPlayer
    receive: MFLPlayer
    own_gain: float
    other_gain: float


@dataclass(frozen=True)
class TargetOffer:
    give: tuple[MFLPlayer, ...]
    receive: MFLPlayer
    own_before: float
    own_after: float
    other_before: float
    other_after: float
    give_projection: float
    receive_projection: float
    label: str

    @property
    def own_gain(self):
        return round(self.own_after - self.own_before, 1)

    @property
    def other_gain(self):
        return round(self.other_after - self.other_before, 1)

    @property
    def give_ids(self):
        return ",".join(p.id for p in self.give)


def analyze_target_trade(own, target, player_id, rosters, catalog, projections, settings,
                         *, package_size=2, limit=5, seconds=8):
    """Rank 1-for-1 and 2-for-1 offers for a requested player.

    Counterfactual legal-lineup optimization is performed for BOTH teams before
    and after every candidate. This is a weekly decision model, not a learned
    acceptance model. Missing projections are excluded and disclosed, not zeroed.
    """
    if own == target or target not in rosters or own not in rosters:
        raise ValueError("Choose another team in this league.")
    own_ids, other_ids = set(rosters[own]), set(rosters[target])
    if player_id not in other_ids or player_id in own_ids or player_id not in catalog:
        raise ValueError("That player is no longer on the selected team. Reload its roster.")
    if package_size not in {1, 2}:
        raise ValueError("Choose one-player or up-to-two-player offers.")
    known = {}
    for pid, raw in projections.items():
        try:
            value = float(raw)
            if math.isfinite(value) and pid in catalog:
                known[pid] = value
        except (ValueError, TypeError):
            continue
    missing = sorted((own_ids | other_ids) - known.keys())
    result = {"offers": [], "examined": 0, "truncated": False, "missing": len(missing),
              "warning": "", "player": catalog[player_id]}
    if player_id not in known:
        result["warning"] = "MFL has no weekly projection for this player. Offers cannot be ranked yet."
        return result
    deadline, cache = time.monotonic() + seconds, {}

    def optimize(ids):
        eligible = frozenset(set(ids) & known.keys())
        if eligible not in cache:
            if time.monotonic() >= deadline:
                result["truncated"] = True
                return None
            starters = _solve_lineup([catalog[p] for p in sorted(eligible)], settings, known, set())
            cache[eligible] = round(sum(known[p] for p in starters), 3) if starters else None
        return cache[eligible]

    before_own, before_other = optimize(own_ids), optimize(other_ids)
    if before_own is None or before_other is None:
        result["warning"] = "There are not enough projected, eligible players to compare both legal lineups, or the search timed out."
        return result
    if missing:
        result["warning"] = f"{len(missing)} rostered players lack usable projections and were excluded. Lineup gains may change when their projections arrive."
    available = sorted((own_ids - other_ids) & known.keys(), key=lambda p:(abs(known[p]-known[player_id]),p))
    # Assess all supported single-player offers, then a shortlist of plausible
    # packages. Bound CPU use even with very large dynasty rosters.
    packages = [(pid,) for pid in available[:30]]
    if package_size == 2:
        pairs = sorted(combinations(available[:14],2), key=lambda pair:(abs(sum(known[p] for p in pair)-known[player_id]),pair))
        packages.extend(pairs[:35])
    result["truncated"] = len(available) > 30 or (package_size == 2 and len(available) > 9)
    candidates = []
    for package in packages:
        if time.monotonic() >= deadline:
            result["truncated"] = True
            break
        outgoing = set(package)
        after_own = optimize(own_ids - outgoing | {player_id})
        after_other = optimize(other_ids - {player_id} | outgoing)
        if after_own is None or after_other is None:
            continue
        result["examined"] += 1
        a, b = after_own-before_own, after_other-before_other
        label = ("Both starting lineups improve" if a >= .5 and b >= .5 else
                 "Your lineup improves; theirs holds steady" if a >= .5 and b >= -.1 else
                 "Their lineup improves; yours holds steady" if b >= .5 and a >= -.1 else
                 "Negotiation starting point — review the trade-off")
        offer = TargetOffer(tuple(catalog[p] for p in package), catalog[player_id],
                            before_own, after_own, before_other, after_other,
                            round(sum(known[p] for p in package),1),known[player_id],label)
        # Mutual gain comes first. Then minimize the worse team's starting
        # lineup loss, favor combined gains and fewer outgoing roster spots.
        rank = (int(a < -.1 or b < -.1), -min(a,b), -(a+b), len(package),
                abs(offer.give_projection-offer.receive_projection),package)
        candidates.append((rank,offer))
    result["offers"] = [offer for _,offer in sorted(candidates,key=lambda item:item[0])[:limit]]
    return result


def suggest_trades(own, rosters, catalog, projections, settings, *, limit=5, seconds=8):
    """Compare legal optimal starting totals for both teams; never invent scores.

    Shortlist surplus players, then score swaps against actual league slot rules.
    Teams missing any player projection are skipped instead of treated as zero.
    Search is capped by candidates and wall time, so these are ideas, not exhaustive.
    """
    deadline = time.monotonic() + seconds
    known = {pid: float(value) for pid, value in projections.items()
             if value is not None and math.isfinite(float(value))}
    cache = {}

    def optimal(ids):
        key = frozenset(ids)
        if key not in cache:
            if not key <= known.keys() or not key <= catalog.keys() or time.monotonic() >= deadline:
                return None
            starters = _solve_lineup([catalog[p] for p in sorted(key)], settings, known, set())
            cache[key] = (sum(known[p] for p in starters), starters) if starters else None
        return cache.get(key)

    own_ids = set(rosters.get(own, ()))
    baseline = optimal(own_ids)
    if baseline is None:
        return []
    surplus = sorted(own_ids - baseline[1], key=lambda p: (-known[p], p))[:4]
    ideas, evaluated = [], 0
    for target, raw_ids in sorted(rosters.items()):
        if target == own or target == "0000" or time.monotonic() >= deadline:
            continue
        ids = set(raw_ids)
        other = optimal(ids)
        if other is None:
            continue
        available = sorted(ids - other[1], key=lambda p: (-known[p], p))[:4]
        for give in surplus:
            for receive in available:
                if evaluated >= 40 or time.monotonic() >= deadline:
                    return sorted(ideas, key=lambda i: (-(i.own_gain + i.other_gain), i.target, i.give.id))[:limit]
                if give in ids or receive in own_ids or catalog[give].position == catalog[receive].position:
                    continue
                evaluated += 1
                after_own = optimal(own_ids - {give} | {receive})
                after_other = optimal(ids - {receive} | {give})
                if after_own and after_other:
                    a, b = after_own[0] - baseline[0], after_other[0] - other[0]
                    if a >= .5 and b >= .5:
                        ideas.append(TradeIdea(target, catalog[give], catalog[receive], round(a, 1), round(b, 1)))
    return sorted(ideas, key=lambda i: (-(i.own_gain + i.other_gain), i.target, i.give.id))[:limit]
