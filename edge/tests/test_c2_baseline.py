"""
Unit & Comparative Tests for Task 3.4:
Baseline C2 (Cloud-Only Relay) and C3 (Deterministic Rules) Modes vs C1 (Leased Edge SLM).
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import http.server
import json
import pytest
import socket
import sys
import threading

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


# --- a tiny mock cloud that answers POST /relay/decide (C2's per-step round trip) ------------

class _RelayCloud(http.server.BaseHTTPRequestHandler):
    """Answers /relay/decide with a scripted decision and records every call.
    The class attribute `decision` is what /relay/decide returns; `calls`
    collects the request bodies so a test can assert the round trip happened."""

    decision = {"action": "wait", "via_zones": [], "rationale": "mock: hold", "source": "llm", "lease_version": 1}
    calls = []
    paths = []

    def log_message(self, *a):
        pass

    def _json(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode("utf-8"))

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode("utf-8") or "{}") if length else {}
        _RelayCloud.paths.append(self.path)
        if self.path == "/relay/decide":
            _RelayCloud.calls.append(body)
            self._json(201, {"duplicate": False, **_RelayCloud.decision})
        else:
            # check-ins, heartbeats, escalations: accept so the run can proceed
            self._json(201, {"accepted": True, "duplicate": False})

    def do_GET(self):
        self._json(200, {"outbox": []})


@pytest.fixture
def relay_cloud():
    _RelayCloud.calls = []
    _RelayCloud.paths = []
    _RelayCloud.decision = {"action": "wait", "via_zones": [], "rationale": "mock: hold",
                            "source": "llm", "lease_version": 1}
    srv = http.server.HTTPServer(("127.0.0.1", 0), _RelayCloud)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def _dead_cloud_url():
    """A URL whose port is closed, so a relay POST fails fast (cloud unreachable)."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


# --- C2 relays each non-reflex step to the cloud and does what it says ------------------------

def test_c2_relays_blocked_step_to_cloud_and_waits(tmp_path, relay_cloud):
    """C2 has no local brain: a blocked step triggers one round trip to the cloud,
    which here answers 'wait'. The robot waits (the transient obstacle clears) and
    the task completes -- driven entirely by the cloud's decision."""
    _RelayCloud.decision = {"action": "wait", "via_zones": [], "rationale": "trolley will move",
                            "source": "llm", "lease_version": 1}
    db_file = str(tmp_path / "test_c2_wait.db")
    sup = EdgeSupervisor(cluster_id="east-wing", db_path=db_file, system_mode="C2", cloud_url=relay_cloud)
    sup.robots["R1"].inject_obstacle("corridor-2B", "transient", "Delivery trolley", clears_after_waits=1)

    res = sup.process_lease(_make_sample_lease(lease_id="lease-taskc2wait-v1"))

    assert res["status"] == "COMPLETED"                       # the cloud decided, the robot obeyed
    assert any(c["step_index"] is not None for c in _RelayCloud.calls)   # the round trip happened
    assert _RelayCloud.calls[0]["cluster_id"] == "east-wing"
    assert _RelayCloud.calls[0]["blocked_zones"] == ["corridor-2B"]
    decisions = sup.store.get_decisions("lease-taskc2wait-v1")
    assert any(d["decided_by"] == "C2_RELAY" for d in decisions)


def test_c2_relays_and_aborts_to_operator_when_cloud_says_abort(tmp_path, relay_cloud):
    """When the cloud's relay answer is 'abort', the C2 edge hands the event off
    (UNEXPECTED_EVENT) rather than resolving anything locally."""
    _RelayCloud.decision = {"action": "abort", "via_zones": [], "rationale": "no route; operator",
                            "source": "rules", "lease_version": 1}
    db_file = str(tmp_path / "test_c2_abort.db")
    sup = EdgeSupervisor(cluster_id="east-wing", db_path=db_file, system_mode="C2", cloud_url=relay_cloud)
    sup.robots["R1"].inject_obstacle("corridor-2B", "transient", "Blocked", clears_after_waits=5)

    res = sup.process_lease(_make_sample_lease(lease_id="lease-taskc2abort-v1"))

    assert res["status"] == "ESCALATED_UNEXPECTED"
    assert len(_RelayCloud.calls) >= 1                        # it asked the cloud first
    assert "/escalations" in _RelayCloud.paths               # then handed the event off


def test_c2_without_a_reachable_cloud_cannot_decide_and_hands_off(tmp_path):
    """A thin relay has no local brain: with the cloud unreachable, C2 must halt
    and escalate -- it must NOT wait the obstacle out the way C1's edge would, or
    it would gain the local autonomy the baseline is defined not to have."""
    db_file = str(tmp_path / "test_c2_off.db")
    sup = EdgeSupervisor(cluster_id="east-wing", db_path=db_file, system_mode="C2", cloud_url=_dead_cloud_url())
    sup.robots["R1"].inject_obstacle("corridor-2B", "transient", "Trolley", clears_after_waits=1)

    res = sup.process_lease(_make_sample_lease(lease_id="lease-taskc2off-v1"))

    assert res["status"] == "ESCALATED_UNEXPECTED"           # stalled on a bad link, by design
    decisions = sup.store.get_decisions("lease-taskc2off-v1")
    assert any(d["decided_by"] == "C2_RELAY_OFFLINE" for d in decisions)
    pending = sup.store.get_pending_outbox()
    assert any(o["message_type"] == "UNEXPECTED_EVENT" for o in pending)


def test_c1_vs_c2_comparative_behavior(tmp_path):
    """Direct comparison under an identical transient obstacle, both offline:
    - C1 (edge autonomy) adapts locally via LOCAL_WAIT and completes with no cloud.
    - C2 (cloud-only relay) has no local brain, so with no cloud it halts and
      hands off. C2's recovery depends on the cloud; C1's does not."""
    db_c1 = str(tmp_path / "test_compare_c1.db")
    sup_c1 = EdgeSupervisor(cluster_id="east-wing", db_path=db_c1, system_mode="C1", slm_mode="mock")
    sup_c1.robots["R1"].inject_obstacle("corridor-2B", "transient", "Mobile trolley", clears_after_waits=1)
    res_c1 = sup_c1.process_lease(_make_sample_lease("lease-c1-cmp"))
    assert res_c1["status"] == "COMPLETED"                   # C1 resolves locally

    db_c2 = str(tmp_path / "test_compare_c2.db")
    sup_c2 = EdgeSupervisor(cluster_id="east-wing", db_path=db_c2, system_mode="C2", cloud_url=_dead_cloud_url())
    sup_c2.robots["R1"].inject_obstacle("corridor-2B", "transient", "Mobile trolley", clears_after_waits=1)
    res_c2 = sup_c2.process_lease(_make_sample_lease("lease-taskc2cmp-v1"))
    assert res_c2["status"] == "ESCALATED_UNEXPECTED"        # C2 cannot act without the cloud


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
