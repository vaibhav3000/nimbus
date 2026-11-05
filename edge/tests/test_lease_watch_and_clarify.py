"""Edge during a lease: the watcher (newer lease / 409 / renewal) and the CLARIFY round trip (PRD FR-8, FR-9, FR-16, FR-22).

The cloud is replaced by monkeypatching `_fetch_outbox_items` (what the edge reads) and `send_checkin_to_cloud`.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import threading
import time

import pytest

_current = Path(__file__).resolve().parent
while _current.parent != _current:
    if (_current / "edge" / "supervisor").is_dir():
        if str(_current) not in sys.path:
            sys.path.insert(0, str(_current))
        break
    _current = _current.parent

from edge.supervisor.supervisor import EdgeSupervisor
from edge.supervisor.artifacts import ArtifactPackager
from edge.schemas.validator import validate_escalation


@pytest.fixture
def sup(tmp_path, monkeypatch):
    s = EdgeSupervisor(cluster_id="east-wing", cloud_url="http://127.0.0.1:9", db_path=str(tmp_path / "t.db"), slm_mode="mock")
    s.artifact_packager = ArtifactPackager(artifacts_dir=str(tmp_path / "art"))
    monkeypatch.setattr(s, "send_checkin_to_cloud", lambda lease=None: (True, "accepted", {}))
    monkeypatch.setattr(s, "_fetch_outbox_items", lambda: [])

    def slow(robot):          # the mock robot is instantaneous; make each command take a moment so the watcher can act mid-lease
        orig = robot.execute_command
        def run(cmd):
            if cmd.get("action") != "stop":
                time.sleep(0.4)
            return orig(cmd)
        robot.execute_command = run
        return robot
    for r in list(s.robots.values()):
        slow(r)
    return s


def _lease(steps, lease_id="lease-task-w-v1", version=1, allowed=None):
    now = datetime.now(timezone.utc)
    return {
        "lease_id": lease_id, "version": version, "priority": "routine",
        "deadline_ts": (now + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expiry_ts": (now + timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "allowed_zones": allowed or ["south_equipment_approach", "east_mid_hall", "east_waiting_approach", "nurse_station_approach"],
        "forbidden_zones": [], "speed_limit": 1.0, "assigned_robots": ["R2"], "task_sequence": steps,
        "cluster_id": "east-wing", "when_to_escalate": ["robot_failed"],
    }


def _later(seconds, fn):
    t = threading.Timer(seconds, fn)
    t.daemon = True
    t.start()


def test_newer_lease_version_in_outbox_stops_the_running_lease(sup, monkeypatch):
    """Operator cancel / re-plan: a higher version of the same task is seen within ~2 s and the lease is dropped."""
    lease = _lease([{"cmd": "wait", "robot": "R2", "params": {"seconds": 1}} for _ in range(40)])
    newer = {"topic": "nimbus/east-wing/lease", "payload": {**lease, "lease_id": "lease-task-w-v2", "version": 2,
             "task_sequence": [{"cmd": "stop", "robot": "R2", "params": {}}]}}
    state = {"on": False}
    monkeypatch.setattr(sup, "_fetch_outbox_items", lambda: [newer] if state["on"] else [])
    _later(1.0, lambda: state.update(on=True))
    t0 = time.time()
    res = sup.process_lease(lease)
    assert res["status"] == "SUPERSEDED" and "v2" in res["reason"]
    assert time.time() - t0 < 15          # not the whole 40-step lease


def test_stale_version_409_on_checkin_stops_the_running_lease(sup, monkeypatch):
    lease = _lease([{"cmd": "wait", "robot": "R2", "params": {"seconds": 1}} for _ in range(40)])
    monkeypatch.setattr(sup, "send_checkin_to_cloud", lambda l=None: (False, "stale_version", {"current": 3}))
    t0 = time.time()
    res = sup.process_lease(lease)
    assert res["status"] in ("SUPERSEDED", "DROPPED_STALE")
    assert time.time() - t0 < 20


def test_renewal_message_extends_the_expiry(sup, monkeypatch):
    lease = _lease([{"cmd": "wait", "robot": "R2", "params": {"seconds": 1}} for _ in range(9)])
    new_exp = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    item = {"topic": "nimbus/east-wing/renewal", "payload": {"lease_id": lease["lease_id"], "version": 1, "expiry_ts": new_exp, "message_id": "r1"}}
    monkeypatch.setattr(sup, "_fetch_outbox_items", lambda: [item])
    sup.process_lease(lease)
    assert lease["expiry_ts"] == new_exp


def test_unknown_destination_sends_clarify_and_resumes_with_the_answer(sup, monkeypatch):
    """FR-9: the edge cannot resolve 'ward-9Z' -> CLARIFY with candidate zones; after the cloud's ANSWER it resumes there."""
    lease = _lease([{"cmd": "go_to", "robot": "R2", "params": {"to": "ward-9Z"}}])
    answer = {"topic": "nimbus/east-wing/notice", "payload": {"type": "ANSWER", "text": "east_waiting_approach",
              "task_id": "task-w", "lease_version": 1, "message_id": "ans-1", "cluster_id": "east-wing"}}
    state = {"on": False}
    monkeypatch.setattr(sup, "_fetch_outbox_items", lambda: [answer] if state["on"] else [])
    _later(1.5, lambda: state.update(on=True))
    res = sup.process_lease(lease)
    pend = [p for p in sup.store.get_pending_outbox() if p["message_type"] == "CLARIFY"]
    assert pend, "CLARIFY must have been queued/sent"
    msg = pend[0]["payload"]
    assert validate_escalation(msg)[0] and msg["task_id"] == "task-w"
    assert "ward-9Z" in msg["payload"]["question"] and msg["payload"]["possible_readings"]
    assert res["status"] == "COMPLETED", res
    assert sup.robots["R2"].current_zone == "east_waiting_approach"


def test_clarify_with_unusable_answer_escalates_unexpected_event(sup, monkeypatch):
    lease = _lease([{"cmd": "go_to", "robot": "R2", "params": {"to": "ward-9Z"}}])
    answer = {"topic": "nimbus/east-wing/notice", "payload": {"type": "ANSWER", "text": "somewhere nice", "task_id": "task-w",
              "lease_version": 1, "message_id": "ans-2"}}
    state = {"on": False}
    monkeypatch.setattr(sup, "_fetch_outbox_items", lambda: [answer] if state["on"] else [])
    _later(1.5, lambda: state.update(on=True))
    res = sup.process_lease(lease)
    assert res["status"] == "ESCALATED_UNEXPECTED"


def test_a_new_lease_while_waiting_for_an_answer_supersedes(sup, monkeypatch):
    lease = _lease([{"cmd": "go_to", "robot": "R2", "params": {"to": "ward-9Z"}}])
    newer = {"topic": "nimbus/east-wing/lease", "payload": {**lease, "lease_id": "lease-task-w-v2", "version": 2}}
    state = {"on": False}
    monkeypatch.setattr(sup, "_fetch_outbox_items", lambda: [newer] if state["on"] else [])
    _later(1.5, lambda: state.update(on=True))
    res = sup.process_lease(lease)
    assert res["status"] == "SUPERSEDED"


def test_an_old_answer_already_in_the_outbox_is_not_used(sup, monkeypatch):
    """An ANSWER that existed before we asked belongs to an earlier question and must be ignored."""
    lease = _lease([{"cmd": "go_to", "robot": "R2", "params": {"to": "ward-9Z"}}])
    old = {"topic": "nimbus/east-wing/notice", "payload": {"type": "ANSWER", "text": "east_waiting_approach", "task_id": "task-w",
           "lease_version": 1, "message_id": "ans-old"}}
    monkeypatch.setattr(sup, "_fetch_outbox_items", lambda: [old])
    monkeypatch.setenv("NIMBUS_CLARIFY_TIMEOUT", "3")
    res = sup.process_lease(lease)
    assert res["status"] == "EXPIRED_PARKED"
