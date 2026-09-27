from datetime import timezone

from weekly_projections.decision_simulator import simulate_decision
from weekly_projections.gameday import chart_points, observe_scoring
from weekly_projections.mfl.client import MFLPlayer


def _teams(left=10.0, right=7.0, player=10.0):
    return [
        {"id": "0001", "name": "Blue", "score": left,
         "players": [{"id": "1", "name": "Runner, One", "score": player, "starter": True}]},
        {"id": "0002", "name": "Gold", "score": right,
         "players": [{"id": "2", "name": "Passer, Two", "score": right, "starter": True}]},
    ]


def test_gameday_records_only_observed_score_deltas_and_probability():
    first = observe_scoring(None, _teams(), (55.0, 45.0), observed_at=1000, timezone=timezone.utc)
    second = observe_scoring(first, _teams(16.2, 7.0, 16.2), (68.25, 31.75), observed_at=1060, timezone=timezone.utc)
    event = second["events"][-1]
    assert event["title"] == "Runner, One +6.20 fantasy points"
    assert "10.00 → 16.20" in event["detail"]
    assert len(second["probability"]) == 2
    assert chart_points(second["probability"]).startswith("0.0,")


def test_gameday_does_not_duplicate_unchanged_snapshots():
    state = observe_scoring(None, _teams(), (50.0, 50.0), observed_at=1000, timezone=timezone.utc)
    state = observe_scoring(state, _teams(), (50.0, 50.0), observed_at=1060, timezone=timezone.utc)
    assert len(state["events"]) == 1
    assert len(state["probability"]) == 1


def test_gameday_can_keep_probability_without_mfl_score_events():
    state = observe_scoring(
        None, _teams(), (55.0, 45.0), observed_at=1000, timezone=timezone.utc,
        record_events=False,
    )
    state = observe_scoring(
        state, _teams(16.2, 7.0, 16.2), (68.25, 31.75), observed_at=1060,
        timezone=timezone.utc, record_events=False,
    )
    assert state["events"] == []
    assert len(state["probability"]) == 2


def test_decision_simulator_reports_week_ros_byes_and_playoffs():
    current = MFLPlayer("1", "Current", "RB", "DET")
    proposed = MFLPlayer("2", "Proposed", "RB", "GB")
    schedule = {week: {"DET": "vs CHI", "GB": "@ MIN", **({f"T{i}": "x" for i in range(24)})}
                for week in range(8, 18)}
    schedule[10].pop("GB")
    result = simulate_decision(
        kind="waiver", current=current, proposed=proposed,
        projections={"1": 10.0, "2": 13.5},
        recent_scores={"1": (8, 10, 12), "2": (7, 14, 20)},
        schedule=schedule, current_week=8,
    )
    assert result.weekly_delta == 3.5
    assert result.rest_of_season_delta == 24.5
    assert "Week 10 bye" in result.bye_consequences[1]
    assert len(result.playoff_consequences) == 3
    assert result.proposed.floor == 7


def test_decision_simulator_keeps_missing_projection_honest():
    result = simulate_decision(
        kind="trade", current=MFLPlayer("1", "A", "WR", "DET"),
        proposed=MFLPlayer("2", "B", "WR", "MIN"), projections={},
        recent_scores={}, schedule={}, current_week=5,
    )
    assert result.weekly_delta is None
    assert result.rest_of_season_delta is None
    assert result.current.floor is None
