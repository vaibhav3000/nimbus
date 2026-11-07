"""Regression: an expiry during one long primitive must never complete it."""
from datetime import datetime, timedelta, timezone
import threading
import time

from edge.supervisor.supervisor import EdgeSupervisor


def test_expiry_watchdog_interrupts_an_active_command(tmp_path, monkeypatch):
    supervisor = EdgeSupervisor(cluster_id="east-wing", db_path=str(tmp_path / "edge.db"), slm_mode="mock", verbose=False)
    monkeypatch.setattr(supervisor, "send_checkin_to_cloud", lambda lease=None: (True, "accepted", {}))
    monkeypatch.setattr(supervisor, "_fetch_outbox_items", lambda: [])
    robot = supervisor.robots["R1"]
    original = robot.execute_command
    started = threading.Event()

    def slow_command(command):
        if command.get("action") == "wait":
            started.set()
            # Leave enough time for the real watchdog thread to fire while
            # this primitive is in progress.
            time.sleep(1.8)
        return original(command)

    robot.execute_command = slow_command
    now = datetime.now(timezone.utc)
    lease = {
        "lease_id": "lease-expiry-active-v1", "version": 1, "priority": "urgent",
        "deadline_ts": (now + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expiry_ts": (now + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "allowed_zones": ["north_hall", "east_waiting_approach"], "forbidden_zones": [], "speed_limit": 1.0,
        "assigned_robots": ["R1"], "cluster_id": "east-wing", "when_to_escalate": [],
        "task_sequence": [{"cmd": "wait", "robot": "R1", "params": {"seconds": 1}}],
    }
    result = supervisor.process_lease(lease)

    assert started.is_set()
    assert result["status"] == "EXPIRED_PARKED"
    assert supervisor.store.get_lease("lease-expiry-active-v1")["status"] == "EXPIRED_PARKED"
    assert any(entry["action"] == "stop" for entry in robot.history)
    assert any(entry.get("params", {}).get("reason") == "lease_expiry_safe_park" for entry in robot.history)
