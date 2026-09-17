from dataclasses import replace

import pytest

from weekly_projections.defense_streaming import UNIT_RANKS_2026, rank_defense_streams, defense_waiver_pricing
from weekly_projections.mfl.client import MFLAvailability, MFLPlayer, MFLTransaction
from weekly_projections.recommendations import PlayerRecommendation


def defense(team, *, position="Def", owned=False, locked=False, cant_add=False):
    return PlayerRecommendation(
        MFLPlayer(team, f"{team} Defense", position, team),
        MFLAvailability(team, status="mine" if owned else "locked" if locked else "available", locked=locked, cant_add=cant_add),
        8.0, None, None, "Review fit", "muted", "",
        fantasy_team_id="0001" if owned else "",
    )


def rank(board, games, **kwargs):
    return rank_defense_streams(board, year=kwargs.pop("year", 2026), games=games,
                                opponent_strength=kwargs.pop("opponent_strength", {}), now=100, **kwargs)


def game(opponent, kickoff=200, final=False):
    return {"opponent_team": opponent, "kickoff": kickoff, "final": final}


def test_snapshot_has_complete_unique_verified_unit_ranks():
    assert len(UNIT_RANKS_2026) == 32
    assert sorted(pair[0] for pair in UNIT_RANKS_2026.values()) == list(range(1, 33))
    assert sorted(pair[1] for pair in UNIT_RANKS_2026.values()) == list(range(1, 33))
    assert UNIT_RANKS_2026["HOU"] == (24, 2)
    assert UNIT_RANKS_2026["MIA"] == (32, 32)


def test_talent_and_opposing_offense_both_influence_fit():
    rows = rank([defense("HOU"), defense("MIA")], {"HOU": game("MIA"), "MIA": game("LAR")})
    assert rows[0].item.player.team == "HOU"
    assert rows[0].score == 98.4
    assert rows[1].score == 0
    harder = rank([defense("HOU")], {"HOU": game("LAR")})[0]
    assert harder.score < rows[0].score
    assert harder.talent_rank == 2 and harder.offense_rank == 1
    assert rows[0].item.projection == 8.0  # Never overwrite league fantasy points.


def test_existing_mfl_def_matchup_is_optional_ten_percent_signal():
    board, games = [defense("DET")], {"DET": game("CHI")}
    baseline = rank(board, games)[0]
    favorable = rank(board, games, opponent_strength={"DET": {"rank": 1, "teams": 32, "position": "DEF"}})[0]
    difficult = rank(board, games, opponent_strength={"DET": {"rank": 32, "teams": 32, "position": "DEF"}})[0]
    assert favorable.score > baseline.score > difficult.score
    assert favorable.score - difficult.score == pytest.approx(10)
    assert favorable.league_matchup_rank == 1
    assert "MFL defense matchup #1" in favorable.explanation
    invalid = rank(board, games, opponent_strength={"DET": {"rank": 1, "teams": 32, "position": "QB"}})[0]
    assert invalid.score == baseline.score


def test_only_claimable_team_defenses_and_owned_comparison_are_shown():
    other = replace(defense("BUF", owned=True), availability=MFLAvailability("BUF", status="rostered"))
    rows = rank([defense("DET", owned=True), defense("SEA", locked=True), other,
                 defense("BAL", position="LB"), defense("HOU", cant_add=True)],
                {"DET": game("CHI"), "SEA": game("ARI")})
    assert {row.item.player.team for row in rows} == {"DET", "SEA"}
    assert next(row for row in rows if row.item.player.team == "SEA").item.is_claimable


@pytest.mark.parametrize("position", ["DEF", "Def", "DST", "D/ST"])
def test_team_defense_position_variants(position):
    assert len(rank([defense("DET", position=position)], {"DET": game("CHI")})) == 1


@pytest.mark.parametrize("games,year", [({}, 2026), ({"DET": game("XYZ")}, 2026),
    ({"DET": game("CHI", kickoff=50)}, 2026), ({"DET": game("CHI", final=True)}, 2026),
    ({"DET": game("CHI")}, 2027)])
def test_unavailable_inputs_and_started_games_do_not_get_fabricated_scores(games, year):
    row = rank([defense("DET")], games, year=year)[0]
    assert row.score is None
    if year != 2026:
        assert row.talent_rank is None and row.offense_rank is None


def test_mfl_team_aliases_and_display_opponents_are_normalized():
    row = rank([defense("HST")], {"HOU": {"opponent": "@ LVR", "kickoff": 200}})[0]
    assert row.talent_rank == 2 and row.offense_rank == 28
    assert row.opponent == "LV"


def test_missing_kickoff_discloses_eligibility_uncertainty():
    row = rank([defense("DET")], {"DET": game("CHI", kickoff="unavailable")})[0]
    assert "Kickoff time unavailable" in row.explanation


def pricing(balance=40, transactions=()):
    streams = rank([defense("HOU", locked=True)], {"HOU": game("MIA")})
    return defense_waiver_pricing(streams, balance=balance, transactions=transactions,
                                 catalog={"HOU": defense("HOU").player}, now=2_000_000)


def award(id="a1", bid=3, **kwargs):
    return MFLTransaction(id, kwargs.pop("kind", "BBID_WAIVER"), kwargs.pop("timestamp", 1_999_999),
                          ("0002",), kwargs.pop("adds", ("HOU",)), (), bid=bid, **kwargs)


def test_waiver_bid_uses_recent_defense_market_and_remaining_budget():
    result = pricing(transactions=[award(bid=2), award("a2", 4), award("a3", 3)])
    bid = result["bids"]["HOU"]
    assert result["median"] == 3 and result["sample_count"] == 3
    assert bid.suggested == 4 and bid.low <= bid.suggested <= bid.high <= 4
    assert "median 3" in bid.explanation
    assert "winning-bid prediction" in bid.explanation
    assert pricing(balance=2, transactions=[award(bid=10)])["bids"]["HOU"].high <= 2


def test_waiver_history_excludes_ambiguous_non_awarded_stale_and_duplicate_bids():
    result = pricing(transactions=[award(), award(), award("multi", adds=("HOU", "DET")),
        award("pending", kind="BBID_WAIVER_REQUEST"), award("fail", kind="BBID_WAIVER_FAILED"),
        award("fcfs", kind="FREE_AGENT"), award("old", timestamp=1), award("future", timestamp=2_000_001),
        award("missing", bid=None), award("other", adds=("LB",)), award("negative", bid=-1)])
    assert result["sample_count"] == 1
    assert "Small sample" in result["bids"]["HOU"].explanation


@pytest.mark.parametrize("balance", [None, float("nan"), float("inf"), -1])
def test_missing_or_invalid_faab_never_fabricates_a_bid(balance):
    bid = pricing(balance)["bids"]["HOU"]
    assert bid.suggested is None and bid.low is None and bid.high is None


def test_no_history_uses_disclosed_budget_heuristic_and_zero_balance_is_not_hidden():
    assert "Budget-only heuristic" in pricing()["bids"]["HOU"].explanation
    zero = pricing(0)["bids"]["HOU"]
    assert zero.suggested == 0 and "league permits" in zero.explanation


def test_no_pricing_for_fcfs_rostered_or_already_started_streams():
    streams = rank([defense("DET"), defense("SEA", owned=True), defense("HOU", locked=True)],
                   {"DET": game("CHI"), "SEA": game("ARI"), "HOU": game("MIA", kickoff=50)})
    result = defense_waiver_pricing(streams, balance=40, transactions=(), catalog={}, now=100)
    assert set(result["bids"]) == {"HOU"}
    assert result["bids"]["HOU"].suggested is None
