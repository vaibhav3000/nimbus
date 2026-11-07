from __future__ import annotations

from experiments.robot_edge import r6_lease_enforcement as r6


def test_each_invalid_authority_is_blocked_before_task_dispatch(tmp_path):
    for index, condition in enumerate(r6.CONDITIONS, 1):
        outcome, timeline = r6.execute_trial(condition, index, tmp_path / f"{condition}.db")
        assert outcome["unauthorized_robot_executions"] == 0
        assert outcome["supervisor_status"] in {"FAILED_BOUNDARY", "EXPIRED_PARKED", "DROPPED_STALE"}
        assert timeline[0]["event"] == "invalid_authority_submitted"


def test_expiry_only_executes_authorized_safe_park(tmp_path):
    outcome, timeline = r6.execute_trial("expired_lease", 1, tmp_path / "expired.db")
    assert outcome["safe_park_execution_count"] == 1
    robot_targets = [row.get("target") for row in timeline if row["event"] == "robot_command"]
    assert "2B-12" not in robot_targets
