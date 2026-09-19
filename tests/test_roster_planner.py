from __future__ import annotations

from weekly_projections import roster_planner
from weekly_projections.mfl.client import MFLAvailability, MFLLineupRule, MFLLineupSettings, MFLPlayer
from weekly_projections.recommendations import PlayerRecommendation


def recommendation(player, drop, projection, delta):
    return PlayerRecommendation(
        player, MFLAvailability(player.id, status="locked", locked=True),
        projection, delta, drop, "Waiver target", "good", "Synthetic recommendation",
    )


def test_multi_week_plan_finds_bye_gap_playoff_schedule_and_stashes():
    quarterback = MFLPlayer("qb", "Quarterback", "QB", "DET")
    runner = MFLPlayer("rb", "Starter Runner", "RB", "BUF")
    qb_stash = MFLPlayer("qb2", "Bye Cover", "QB", "KC")
    rb_handcuff = MFLPlayer("rb2", "Backup Runner", "RB", "BUF")
    schedule = {}
    for week in (2, 3, 4, 5, 6, 7, 15, 16, 17):
        games = {f"T{index}": "vs X" for index in range(24)}
        games.update({"BUF": "vs MIA", "KC": "@ LV"})
        if week != 2:
            games["DET"] = "@ GB"
        schedule[week] = games
    settings = MFLLineupSettings(2, (MFLLineupRule("QB", 1, 1), MFLLineupRule("RB", 1, 1)))
    plan = roster_planner.build_roster_plan(
        roster=[quarterback, runner], settings=settings,
        projections={"qb": 20, "rb": 12, "qb2": 18, "rb2": 10},
        schedule=schedule, current_week=2,
        recommendations=[
            recommendation(qb_stash, quarterback, 18, 2),
            recommendation(rb_handcuff, runner, 10, 1),
        ],
        depth_ranks={"rb2": 2},
    )
    week_two = next(row for row in plan.week_rows if row.week == 2)
    assert [player.id for player in week_two.bye_players] == ["qb"]
    assert "QB" in week_two.missing_slots
    assert {15, 16, 17}.issubset(plan.weeks)
    assert any(item.kind == "Bye-week stash" and item.player.id == "qb2" for item in plan.stashes)
    assert any(item.kind == "Handcuff" and item.player.id == "rb2" for item in plan.stashes)


def test_partial_schedule_never_infers_a_bye():
    player = MFLPlayer("p", "Player", "WR", "DET")
    settings = MFLLineupSettings(1, (MFLLineupRule("WR", 1, 1),))
    plan = roster_planner.build_roster_plan(
        roster=[player], settings=settings, projections={}, schedule={2: {"BUF": "vs MIA"}},
        current_week=2, recommendations=[],
    )
    assert plan.week_rows[0].schedule_available is False
    assert plan.week_rows[0].bye_players == ()


def test_schedule_download_is_bounded_and_parses_opponents(monkeypatch):
    roster_planner._schedule_cache.clear()
    payload = (
        "season,game_type,week,home_team,away_team\n"
        "2026,REG,2,DET,GB\n2025,REG,2,BUF,MIA\n"
    ).encode()

    class Response:
        content = payload
        def raise_for_status(self): return None

    calls = []
    monkeypatch.setattr(roster_planner.requests, "get", lambda *args, **kwargs: calls.append(kwargs) or Response())
    schedule = roster_planner.load_nfl_schedule(2026)
    assert schedule[2] == {"DET": "vs GB", "GB": "@ DET"}
    assert calls[0]["timeout"] == (3.05, 25)
    assert roster_planner.load_nfl_schedule(2026) is schedule
    assert len(calls) == 1
