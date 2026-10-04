from weekly_projections.lineup import (
    injury_replacement_plan,
    lineup_is_legal,
    recommend_lineup,
)
from weekly_projections.mfl.client import (
    MFLInjury,
    MFLLineupRule,
    MFLLineupSettings,
    MFLPlayer,
)


def _settings() -> MFLLineupSettings:
    return MFLLineupSettings(
        starter_count=4,
        rules=(
            MFLLineupRule("QB", 1, 1),
            MFLLineupRule("RB", 1, 2),
            MFLLineupRule("WR", 1, 2),
        ),
    )


def test_optimizer_obeys_flexible_league_limits() -> None:
    roster = [
        MFLPlayer("qb1", "Quarterback", "QB"),
        MFLPlayer("rb1", "Running Back One", "RB"),
        MFLPlayer("rb2", "Running Back Two", "RB"),
        MFLPlayer("wr1", "Receiver One", "WR"),
        MFLPlayer("wr2", "Receiver Two", "WR"),
    ]
    result = recommend_lineup(
        roster=roster,
        settings=_settings(),
        projections={"qb1": 20, "rb1": 14, "rb2": 13, "wr1": 12, "wr2": 8},
        roster_statuses={"qb1": "S", "rb1": "S", "wr1": "S", "wr2": "S", "rb2": "NS"},
        injuries={},
    )
    assert result.recommended_starters == {"qb1", "rb1", "rb2", "wr1"}
    assert result.projected_gain == 5
    chosen = [row.player for row in result.players if row.recommended_start]
    assert lineup_is_legal(chosen, _settings())


def test_optimizer_sits_out_player_when_healthy_option_exists() -> None:
    settings = MFLLineupSettings(
        starter_count=1,
        rules=(MFLLineupRule("WR", 1, 1),),
    )
    result = recommend_lineup(
        roster=[
            MFLPlayer("out", "Unavailable Star", "WR"),
            MFLPlayer("healthy", "Healthy Backup", "WR"),
        ],
        settings=settings,
        projections={"out": 18, "healthy": 10},
        roster_statuses={"out": "S", "healthy": "NS"},
        injuries={"out": MFLInjury("out", "Out", "Hamstring")},
    )
    assert result.recommended_starters == {"healthy"}
    actions = {row.player.id: row.action for row in result.players}
    assert actions == {"healthy": "START", "out": "SIT"}


def test_optimizer_never_moves_players_after_kickoff() -> None:
    settings = MFLLineupSettings(
        starter_count=1,
        rules=(MFLLineupRule("WR", 1, 1),),
    )
    result = recommend_lineup(
        roster=[
            MFLPlayer("locked", "Locked Starter", "WR"),
            MFLPlayer("better", "Higher Projection", "WR"),
        ],
        settings=settings,
        projections={"locked": 8, "better": 20},
        roster_statuses={"locked": "S", "better": "NS"},
        injuries={},
        locked_player_ids={"locked"},
    )
    assert result.recommended_starters == {"locked"}
    actions = {row.player.id: row.action for row in result.players}
    assert actions["locked"] == "Locked starter"


def test_position_aliases_accept_mfl_idp_and_kicker_positions() -> None:
    players = [MFLPlayer("dl","Defender","DL"), MFLPlayer("k","Kicker","PK")]
    settings = MFLLineupSettings(2, (MFLLineupRule("DL",1,1), MFLLineupRule("K",1,1)))
    assert lineup_is_legal(players, settings)


def test_infeasible_recommendation_preserves_saved_locked_lineup():
    players = [MFLPlayer("a","Locked starter","WR"), MFLPlayer("b","Locked bench","WR")]
    result = recommend_lineup(
        roster=players, settings=MFLLineupSettings(2,(MFLLineupRule("WR",2,2),)),
        projections={"a":8,"b":30}, roster_statuses={"a":"S","b":"NS"},
        injuries={}, locked_player_ids={"a","b"},
    )
    assert not result.used_league_rules
    assert result.recommended_starters == {"a"}


def test_emergency_plan_replaces_only_out_starter_with_highest_legal_bench_player():
    settings = MFLLineupSettings(
        3,
        (
            MFLLineupRule("QB", 1, 1),
            MFLLineupRule("RB", 1, 1),
            MFLLineupRule("WR", 1, 1),
        ),
    )
    roster = [
        MFLPlayer("qb", "Saved QB", "QB"),
        MFLPlayer("rb", "Saved RB", "RB"),
        MFLPlayer("out", "Out Receiver", "WR"),
        MFLPlayer("high", "High Receiver", "WR"),
        MFLPlayer("low", "Low Receiver", "WR"),
        MFLPlayer("tempting", "Tempting RB", "RB"),
    ]
    recommendation = recommend_lineup(
        roster=roster,
        settings=settings,
        projections={"qb": 15, "rb": 5, "out": 18, "high": 12, "low": 8, "tempting": 30},
        roster_statuses={
            "qb": "S", "rb": "S", "out": "S", "high": "NS", "low": "NS", "tempting": "NS",
        },
        injuries={"out": MFLInjury("out", "Out", "Hamstring")},
    )

    plan = injury_replacement_plan(recommendation, settings, {"out"})

    assert plan is not None
    assert plan.starter_ids == {"qb", "rb", "high"}
    assert [item.player.id for item in plan.unavailable[0].candidates] == ["high", "low"]


def test_emergency_plan_fails_closed_when_only_replacement_is_locked():
    settings = MFLLineupSettings(1, (MFLLineupRule("WR", 1, 1),))
    recommendation = recommend_lineup(
        roster=[MFLPlayer("out", "Out", "WR"), MFLPlayer("locked", "Locked", "WR")],
        settings=settings,
        projections={"out": 12, "locked": 10},
        roster_statuses={"out": "S", "locked": "NS"},
        injuries={"out": MFLInjury("out", "Inactive", "")},
        locked_player_ids={"locked"},
    )

    assert injury_replacement_plan(recommendation, settings, {"out"}) is None


def test_emergency_plan_uses_owner_priority_before_projection():
    settings = MFLLineupSettings(1, (MFLLineupRule("WR", 1, 1),))
    recommendation = recommend_lineup(
        roster=[
            MFLPlayer("out", "Out", "WR"),
            MFLPlayer("favorite", "Preferred Backup", "WR"),
            MFLPlayer("higher", "Higher Projection", "WR"),
        ],
        settings=settings,
        projections={"out": 18, "favorite": 7, "higher": 14},
        roster_statuses={"out": "S", "favorite": "NS", "higher": "NS"},
        injuries={"out": MFLInjury("out", "Out", "")},
    )

    plan = injury_replacement_plan(
        recommendation, settings, {"out"}, ["favorite", "higher"],
    )

    assert plan is not None
    assert plan.starter_ids == {"favorite"}
    assert [item.player.id for item in plan.unavailable[0].candidates] == ["favorite", "higher"]


def test_emergency_plan_reassigns_healthy_flex_rb_to_replace_out_rb():
    settings = MFLLineupSettings(
        starter_count=3,
        rules=(
            MFLLineupRule("RB", 1, 2),
            MFLLineupRule("WR", 1, 2),
        ),
    )
    recommendation = recommend_lineup(
        roster=[
            MFLPlayer("out-rb", "Out Starting Runner", "RB"),
            MFLPlayer("flex-rb", "Healthy Flex Runner", "RB"),
            MFLPlayer("starting-wr", "Healthy Starting Receiver", "WR"),
            MFLPlayer("bench-wr", "Preferred Bench Receiver", "WR"),
            MFLPlayer("bench-rb", "Lower Rated Bench Runner", "RB"),
        ],
        settings=settings,
        projections={
            "out-rb": 18,
            "flex-rb": 15,
            "starting-wr": 14,
            "bench-wr": 13,
            "bench-rb": 8,
        },
        roster_statuses={
            "out-rb": "S",
            "flex-rb": "S",
            "starting-wr": "S",
            "bench-wr": "NS",
            "bench-rb": "NS",
        },
        injuries={"out-rb": MFLInjury("out-rb", "Out", "")},
    )

    plan = injury_replacement_plan(recommendation, settings, {"out-rb"})

    assert plan is not None
    assert plan.starter_ids == {"flex-rb", "starting-wr", "bench-wr"}
    assert [item.player.id for item in plan.unavailable[0].candidates] == [
        "bench-wr",
        "bench-rb",
    ]
