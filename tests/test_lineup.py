from weekly_projections.lineup import lineup_is_legal, recommend_lineup
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
