"""
Unit & Integration Tests for Task 3.1:
Edge Node Failover & High Availability (FR-7, FR-8, E5).

Verifies that if a primary edge supervisor crashes mid-mission:
1. A standby supervisor pointing to the shared SQLite state resumes within < 30 seconds (FR-7).
2. Already-completed sub-tasks are detected and skipped with zero duplicate physical actions (FR-8).
3. The remaining sub-tasks complete and the final delivery proof is generated.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
import sys
import time

_current = Path(__file__).resolve().parent
while _current.parent != _current:
    if (_current / "edge" / "supervisor").is_dir():
        if str(_current) not in sys.path:
            sys.path.insert(0, str(_current))
        break
    _current = _current.parent

from edge.supervisor.supervisor import EdgeSupervisor


def _make_multi_step_lease(lease_id="lease-failover-001", version=1):
    now = datetime.now(timezone.utc)
    deadline = now + timedelta(minutes=10)
    expiry = now + timedelta(minutes=15)
    return {
        "lease_id": lease_id,
        "version": version,
        "priority": "urgent",
        "deadline_ts": deadline.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expiry_ts": expiry.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "allowed_zones": ["central-supply", "corridor-2B", "2B-12"],
        "forbidden_zones": ["ward-2C-isolation"],
        "speed_limit": 0.8,
        "assigned_robots": ["R1"],
        "task_sequence": [
            {"cmd": "go_to", "robot": "R1", "params": {"to": "central-supply"}},
            {"cmd": "wait", "robot": "R1", "params": {"duration_sec": 1.0}},
            {"cmd": "go_to", "robot": "R1", "params": {"to": "2B-12"}},
            {"cmd": "photo", "robot": "R1", "params": {}},
        ],
        "cluster_id": "east-wing",
        "when_to_escalate": ["door_closed", "deadline_at_risk", "robot_failed"],
    }


def test_standby_takeover_resumes_without_duplicate_steps(tmp_path):
    """
    FR-7: Primary supervisor dies after executing Steps 1 & 2.
    Standby supervisor boots, detects in-flight active lease, skips Steps 1 & 2
    (zero duplicate commands), executes Steps 3 & 4, and completes the mission.
    """
    shared_db = str(tmp_path / "shared_edge_cluster.db")

    # 1. Initialize Primary Supervisor
    primary_sup = EdgeSupervisor(cluster_id="east-wing", db_path=shared_db, slm_mode="mock")
    lease = _make_multi_step_lease("lease-fo-01")

    # Ingest lease into shared SQLite
    primary_sup.store.store_lease(lease)
    primary_sup.active_lease = lease

    # Primary executes Step 1 (go_to central-supply) and Step 2 (wait)
    r1_primary = primary_sup.robots["R1"]

    cmd1 = {
        "cmd_id": "cmd-lease-fo-01-step-1",
        "lease_id": "lease-fo-01",
        "robot_id": "R1",
        "action": "go_to",
        "params": {"to": "central-supply"},
        "timestamp": time.time(),
    }
    primary_sup.store.store_robot_command(cmd1)
    primary_sup.store.update_robot_command_status(cmd1["cmd_id"], "DISPATCHED")
    r1_primary.execute_command(cmd1)
    primary_sup.store.update_robot_command_status(cmd1["cmd_id"], "COMPLETED")

    cmd2 = {
        "cmd_id": "cmd-lease-fo-01-step-2",
        "lease_id": "lease-fo-01",
        "robot_id": "R1",
        "action": "wait",
        "params": {"duration_sec": 0.5},
        "timestamp": time.time(),
    }
    primary_sup.store.store_robot_command(cmd2)
    primary_sup.store.update_robot_command_status(cmd2["cmd_id"], "DISPATCHED")
    r1_primary.execute_command(cmd2)
    primary_sup.store.update_robot_command_status(cmd2["cmd_id"], "COMPLETED")

    # Record command execution history length on Primary
    primary_cmd_count = len(r1_primary.history)
    assert primary_cmd_count == 2

    # --- SIMULATE PRIMARY PROCESS CRASH (SIGKILL) ---
    del primary_sup

    # 2. Standby Supervisor takes over
    t_start = time.time()
    standby_sup = EdgeSupervisor(cluster_id="east-wing", db_path=shared_db, slm_mode="mock")

    # Standby discovers active leases and resumes
    takeover_results = standby_sup.resume_from_failover()
    takeover_time = time.time() - t_start

    # FR-7 SLA: Takeover must complete well within 30 seconds
    assert takeover_time < 30.0, f"Takeover took {takeover_time:.2f}s, exceeding 30s SLA"
    assert len(takeover_results) == 1

    res = takeover_results[0]
    assert res["status"] == "COMPLETED"

    # Verify executed steps breakdown:
    # Steps 1 and 2 must be flagged as skipped duplicates
    executed_steps = res["executed_steps"]
    assert len(executed_steps) == 4

    assert executed_steps[0]["skipped_duplicate"] is True
    assert executed_steps[1]["skipped_duplicate"] is True
    assert executed_steps[2].get("skipped_duplicate") is not True
    assert executed_steps[3].get("skipped_duplicate") is not True

    # Standby robot executed only the remaining 2 steps (Steps 3 & 4)
    r1_standby = standby_sup.robots["R1"]
    assert len(r1_standby.history) == 2  # exactly 2 physical commands, ZERO duplicates!

    # Final lease status in DB is COMPLETED
    lease_record = standby_sup.store.get_lease("lease-fo-01")
    assert lease_record is not None
    assert lease_record["status"] == "COMPLETED"


def test_standby_outbox_persistence_across_crash(tmp_path):
    """
    Verifies that pending outbox escalations written before the crash
    survive in SQLite and are accessible to the standby supervisor.
    """
    shared_db = str(tmp_path / "shared_outbox.db")

    # 1. Primary queues an escalation and dies
    primary = EdgeSupervisor(cluster_id="east-wing", db_path=shared_db, slm_mode="mock")
    primary.store.enqueue_outbox(
        message_id="msg-pre-crash-001",
        lease_id="lease-pre-crash",
        message_type="UNEXPECTED_EVENT",
        payload={"what": "Door blocked", "where": "corridor-2B"},
    )
    del primary

    # 2. Standby verifies queue persistence
    standby = EdgeSupervisor(cluster_id="east-wing", db_path=shared_db, slm_mode="mock")
    pending = standby.store.get_pending_outbox()
    assert len(pending) == 1
    assert pending[0]["message_id"] == "msg-pre-crash-001"
    assert pending[0]["message_type"] == "UNEXPECTED_EVENT"
