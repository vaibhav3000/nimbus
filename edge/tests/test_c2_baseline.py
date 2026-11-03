"""
Unit & Comparative Tests for Task 3.4:
Baseline C2 (Cloud-Only Relay) and C3 (Deterministic Rules) Modes vs C1 (Leased Edge SLM).
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
import sys

_current = Path(__file__).resolve().parent
while _current.parent != _current:
    if (_current / "edge" / "supervisor").is_dir():
        if str(_current) not in sys.path:
            sys.path.insert(0, str(_current))
        break
    _current = _current.parent

from edge.supervisor.supervisor import EdgeSupervisor


def _make_sample_lease(lease_id="lease-c2-001", version=1, destination="2B-12"):
    now = datetime.now(timezone.utc)
    deadline = now + timedelta(minutes=10)
    expiry = now + timedelta(minutes=15)
    return {
        "lease_id": lease_id,
        "version": version,
        "priority": "routine",
        "deadline_ts": deadline.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expiry_ts": expiry.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "allowed_zones": ["central-supply", "corridor-2B", "corridor-2B-north", "2B-12"],
        "forbidden_zones": ["ward-2C-isolation"],
        "speed_limit": 0.8,
        "assigned_robots": ["R1", "R2"],
        "task_sequence": [
            {"cmd": "go_to", "robot": "R1", "params": {"to": "central-supply"}},
            {"cmd": "go_to", "robot": "R1", "params": {"to": destination}},
            {"cmd": "photo", "robot": "R1", "params": {}},
        ],
        "cluster_id": "east-wing",
        "when_to_escalate": ["door_closed", "deadline_at_risk", "robot_failed"],
    }


def test_c2_baseline_relays_obstacle_to_cloud(tmp_path):
    """
    In C2 (Cloud-Only Relay) baseline mode, any obstacle encountered in corridor
    bypasses local SLM reasoning and immediately halts the robot, logging an
    escalation to Cloud outbox (ESCALATED_CLOUD).
    """
    db_file = str(tmp_path / "test_c2_obs.db")
    supervisor = EdgeSupervisor(cluster_id="east-wing", db_path=db_file, system_mode="C2")

    # Inject obstacle into corridor-2B
    robot = supervisor.robots["R1"]
    robot.inject_obstacle(
        zone="corridor-2B",
        obstacle_type="transient",
        description="Delivery trolley parked in hallway",
        clears_after_waits=1,
    )

    lease = _make_sample_lease(lease_id="lease-c2-obs")
    res = supervisor.process_lease(lease)

    # In C2 mode, status must be ESCALATED_CLOUD instead of COMPLETED
    assert res["status"] == "ESCALATED_CLOUD"
    assert "C2 Cloud-Only Relay" in res["reason"]
    assert res["blocked_zone"] == "corridor-2B"

    # Verify decision log marks decided_by as RELAY
    decisions = supervisor.store.get_decisions("lease-c2-obs")
    assert len(decisions) >= 1
    assert any(d["decided_by"] == "RELAY" for d in decisions)

    # Verify UNEXPECTED_EVENT escalation was placed in outbox for Cloud
    pending_outbox = supervisor.store.get_pending_outbox()
    assert len(pending_outbox) >= 1
    assert any(o["message_type"] == "UNEXPECTED_EVENT" for o in pending_outbox)


def test_c2_baseline_relays_hardware_fault_to_cloud(tmp_path):
    """
    In C2 mode, encountering a robot hardware fault bypasses SWAP_ROBOT local
    reasoning and immediately forwards an escalation to Cloud.
    """
    db_file = str(tmp_path / "test_c2_fault.db")
    supervisor = EdgeSupervisor(cluster_id="east-wing", db_path=db_file, system_mode="C2")

    # Inject motor fault into primary robot R1
    robot = supervisor.robots["R1"]
    robot.set_fault("Motor drive stall in steering assembly")

    lease = _make_sample_lease(lease_id="lease-c2-fault")
    res = supervisor.process_lease(lease)

    assert res["status"] == "ESCALATED_CLOUD"
    assert "C2 Cloud-Only Relay" in res["reason"]

    # Robot was commanded to halt
    assert robot.status in ("FAILED", "IDLE")


def test_c1_vs_c2_comparative_behavior(tmp_path):
    """
    Direct comparative test:
    Under identical transient obstacle:
    - C1 (Edge Autonomy) adapts locally via LOCAL_WAIT and completes without cloud intervention.
    - C2 (Cloud Relay) halts and pushes escalation to Cloud.
    """
    # 1. Run under C1 (Our system)
    db_c1 = str(tmp_path / "test_compare_c1.db")
    sup_c1 = EdgeSupervisor(cluster_id="east-wing", db_path=db_c1, system_mode="C1", slm_mode="mock")
    sup_c1.robots["R1"].inject_obstacle("corridor-2B", "transient", "Mobile trolley", clears_after_waits=1)

    res_c1 = sup_c1.process_lease(_make_sample_lease("lease-c1-cmp"))
    assert res_c1["status"] == "COMPLETED"  # C1 resolves locally!

    # 2. Run under C2 (Cloud Relay Baseline)
    db_c2 = str(tmp_path / "test_compare_c2.db")
    sup_c2 = EdgeSupervisor(cluster_id="east-wing", db_path=db_c2, system_mode="C2")
    sup_c2.robots["R1"].inject_obstacle("corridor-2B", "transient", "Mobile trolley", clears_after_waits=1)

    res_c2 = sup_c2.process_lease(_make_sample_lease("lease-c2-cmp"))
    assert res_c2["status"] == "ESCALATED_CLOUD"  # C2 is forced to halt & ask Cloud


def test_c3_rules_baseline_mode(tmp_path):
    """
    In C3 mode, supervisor operates with purely deterministic rules (slm_mode='rules_only').
    """
    db_file = str(tmp_path / "test_c3.db")
    supervisor = EdgeSupervisor(cluster_id="east-wing", db_path=db_file, system_mode="C3")
    assert supervisor.slm.mode == "rules_only"

    # Transient obstacle is handled deterministically via rule engine
    supervisor.robots["R1"].inject_obstacle("corridor-2B", "transient", "Trolley", clears_after_waits=1)
    res = supervisor.process_lease(_make_sample_lease("lease-c3-run"))
    assert res["status"] == "COMPLETED"

    decisions = supervisor.store.get_decisions("lease-c3-run")
    assert any(d["decided_by"] == "RULE" for d in decisions)
