"""
Comprehensive PRD & Contract Verification Tests for Person 2 (Edge Tier)
Validates all requirements from PRD v5.0 and docs/p2-edge-contract-ms2.md:
1. Stop Lease recognition & immediate safe parking (FR-5, FR-8, docs Sec 1)
2. Edge Authentication Token / Bearer Header compliance (NFR-5, docs Sec 5)
3. Structured UNEXPECTED_EVENT with segments & robot_zone (FR-9, docs Sec 4)
4. Outage Reconnection & Cloud Reconciliation protocol (FR-12, docs Sec 5)
5. Survey Leases (T7) report compilation & artifact persistence (NFR-7, docs Sec 5)
6. Selective Sharing & Cluster Topic Isolation (FR-11, docs Sec 3)
"""

from datetime import datetime, timedelta, timezone
import http.server
import json
from pathlib import Path
import threading
import time
import pytest

from edge.supervisor.supervisor import EdgeSupervisor
from edge.mock.mock_robot import MockRobot


class MockCloudHandler(http.server.BaseHTTPRequestHandler):
    recorded_requests = []
    reconcile_response = None

    def log_message(self, format, *args):
        pass  # quiet

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        data = self.rfile.read(length) if length > 0 else b"{}"
        body = json.loads(data.decode("utf-8")) if data else {}
        auth = self.headers.get("Authorization")

        MockCloudHandler.recorded_requests.append({
            "path": self.path,
            "method": "POST",
            "headers": dict(self.headers),
            "auth": auth,
            "body": body,
        })

        if self.path == "/fleet/heartbeat":
            resp = {"accepted": True, "health": "healthy"}
            if MockCloudHandler.reconcile_response:
                resp["reconcile"] = MockCloudHandler.reconcile_response
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(resp).encode("utf-8"))
        elif self.path == "/checkins":
            resp = {"accepted": True, "renewed_until": "2026-11-04T12:00:00Z"}
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(resp).encode("utf-8"))
        elif self.path == "/escalations":
            resp = {"accepted": True, "duplicate": False}
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(resp).encode("utf-8"))
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

    def do_GET(self):
        auth = self.headers.get("Authorization")
        MockCloudHandler.recorded_requests.append({
            "path": self.path,
            "method": "GET",
            "headers": dict(self.headers),
            "auth": auth,
            "body": None,
        })
        if self.path.split("?")[0] == "/outbox":
            outbox = [
                {
                    "topic": "nimbus/east-wing/lease",
                    "payload": {
                        "lease_id": "lease-task-100-v1",
                        "version": 1,
                        "cluster_id": "east-wing",
                        "assigned_robots": ["R1"],
                        "allowed_zones": ["central-supply", "2B-12"],
                        "forbidden_zones": ["ward-2C-isolation"],
                        "task_sequence": [{"cmd": "photo", "robot": "R1", "params": {}}],
                    }
                },
                {
                    # Another cluster lease -> should be ignored (FR-11)
                    "topic": "nimbus/west-wing/lease",
                    "payload": {
                        "lease_id": "lease-task-200-v1",
                        "version": 1,
                        "cluster_id": "west-wing",
                    }
                }
            ]
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"outbox": outbox}).encode("utf-8"))
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")


@pytest.fixture
def mock_cloud_server():
    MockCloudHandler.recorded_requests.clear()
    MockCloudHandler.reconcile_response = None
    server = http.server.HTTPServer(("127.0.0.1", 0), MockCloudHandler)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


def test_stop_lease_recognition_and_robot_parking(tmp_path):
    """
    PRD FR-5, FR-8 & docs/p2-edge-contract-ms2.md Sec 1:
    A stop lease has task_sequence == [{"cmd": "stop"}].
    When received:
    1. Drop older version (FR-8) and record highest version in store.
    2. Park the robot at allowed_zones[0].
    3. Do NOT start new sub-tasks, do NOT check in, do NOT send OUTPUT_READY.
    """
    db_file = str(tmp_path / "test_stop.db")
    supervisor = EdgeSupervisor(cluster_id="east-wing", db_path=db_file, slm_mode="mock")

    now = datetime.now(timezone.utc)
    stop_lease = {
        "lease_id": "lease-task-000007-v3",
        "version": 3,
        "priority": "urgent",
        "deadline_ts": (now + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expiry_ts": (now + timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cluster_id": "east-wing",
        "assigned_robots": ["R1"],
        "allowed_zones": ["east_waiting_approach"],
        "forbidden_zones": ["ward-2C-isolation"],
        "speed_limit": 0.5,
        "task_sequence": [{"cmd": "stop", "params": {}}],
        "when_to_escalate": ["door_closed"],
    }

    res = supervisor.process_lease(stop_lease)

    assert res["status"] == "STOPPED"
    assert "Stop lease" in res["reason"]
    assert res["executed_steps_count"] == 0

    # Robot must be parked at east_waiting_approach
    robot = supervisor.robots["R1"]
    assert robot.current_zone == "east_waiting_approach"

    # Monotonic version check in store
    with supervisor.store._get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status, version FROM leases WHERE lease_id = 'lease-task-000007-v3'")
        row = cursor.fetchone()
        assert row is not None
        assert row["status"] == "STOPPED"
        assert row["version"] == 3


def test_edge_token_authentication_headers(tmp_path, mock_cloud_server):
    """
    PRD NFR-5 & docs/p2-edge-contract-ms2.md Sec 5:
    When NIMBUS_EDGE_TOKEN is configured, all cloud requests (heartbeat, checkin,
    outbox poll, escalations) MUST transmit 'Authorization: Bearer <token>'.
    """
    db_file = str(tmp_path / "test_auth.db")
    test_token = "secret-edge-bearer-token-12345"
    supervisor = EdgeSupervisor(
        cluster_id="east-wing",
        cloud_url=mock_cloud_server,
        db_path=db_file,
        slm_mode="mock",
        token=test_token,
    )

    # 1. Heartbeat
    supervisor.send_heartbeat_to_cloud()
    # 2. Outbox fetch
    supervisor.fetch_leases_from_cloud()
    # 3. Check-in
    dummy_lease = {
        "lease_id": "lease-task-000001-v1",
        "version": 1,
        "task_id": "task-000001",
        "cluster_id": "east-wing",
    }
    supervisor.send_checkin_to_cloud(dummy_lease)
    # 4. Escalation
    supervisor.trigger_unexpected_event(
        task_id="task-000001",
        what="Obstacle blocked path",
        where="east_mid_hall",
        lease_version=1,
    )

    expected_auth = f"Bearer {test_token}"
    paths_checked = set()
    for req in MockCloudHandler.recorded_requests:
        path = req["path"]
        assert req["auth"] == expected_auth, f"Request to {path} missing expected auth header!"
        paths_checked.add(path)

    assert "/fleet/heartbeat" in paths_checked
    assert any(p.startswith("/outbox") for p in paths_checked)
    assert "/checkins" in paths_checked
    assert "/escalations" in paths_checked


def test_unexpected_event_structured_segments_and_robot_zone(tmp_path, mock_cloud_server):
    """
    PRD FR-9 & docs/p2-edge-contract-ms2.md Sec 4:
    UNEXPECTED_EVENT must send 'segments': [{'segment_id': <node_id>, 'state': 'blocked'}]
    and optional 'robot_zone' representing the node where the robot parked.
    """
    db_file = str(tmp_path / "test_unexp.db")
    supervisor = EdgeSupervisor(
        cluster_id="east-wing",
        cloud_url=mock_cloud_server,
        db_path=db_file,
        slm_mode="mock",
    )

    supervisor.trigger_unexpected_event(
        task_id="task-000042",
        what="Scaffolding collapsed in corridor",
        where="east_mid_hall",
        robots_saw=["R1 saw collapsed scaffolding at east_mid_hall"],
        lease_version=2,
        robot_zone="east_waiting_approach",
    )

    # Inspect sent payload
    esc_req = [r for r in MockCloudHandler.recorded_requests if r["path"] == "/escalations"][0]
    payload = esc_req["body"]["payload"]
    assert "segments" in payload
    assert len(payload["segments"]) >= 1
    assert payload["segments"][0]["segment_id"] == "east_mid_hall"
    assert payload["segments"][0]["state"] == "blocked"
    assert payload["robot_zone"] == "east_waiting_approach"


def test_reconnection_reconciliation_action_stop(tmp_path, mock_cloud_server):
    """
    PRD FR-12 & docs/p2-edge-contract-ms2.md Sec 5:
    After outage, heartbeat carries held leases. If cloud replies with reconcile: [{'action': 'stop'}],
    the supervisor halts and safely parks the robot immediately.
    """
    db_file = str(tmp_path / "test_reconcile.db")
    supervisor = EdgeSupervisor(
        cluster_id="east-wing",
        cloud_url=mock_cloud_server,
        db_path=db_file,
        slm_mode="mock",
    )

    # Set active lease held by supervisor
    now = datetime.now(timezone.utc)
    active_lease = {
        "lease_id": "lease-task-000088-v1",
        "version": 1,
        "task_id": "task-000088",
        "cluster_id": "east-wing",
        "assigned_robots": ["R1"],
        "allowed_zones": ["north_hall", "east_waiting_approach"],
        "forbidden_zones": ["ward-2C-isolation"],
        "deadline_ts": (now + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expiry_ts": (now + timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "task_sequence": [{"cmd": "go_to", "params": {"to": "east_waiting_approach"}}],
    }
    supervisor.store.store_lease(active_lease)
    supervisor.active_lease = active_lease

    # Configure mock cloud to reply with reconcile: stop
    MockCloudHandler.reconcile_response = [
        {"lease_id": "lease-task-000088-v1", "action": "stop"}
    ]

    ok, hb_data = supervisor.send_heartbeat_to_cloud()
    assert ok is True
    assert "reconcile" in hb_data

    # Active lease must now be terminated
    assert supervisor.active_lease is None


def test_survey_task_report_generation(tmp_path):
    """
    PRD Task T7, NFR-7 & docs/p2-edge-contract-ms2.md Sec 5:
    Survey lease with 'report' step produces survey.json artifact with
    {'reports': [{'robot': 'R1', 'counts': {'<place>': {'<item>': <n>}}}]}
    and stores storage_uri reference for cloud merge.
    """
    db_file = str(tmp_path / "test_survey.db")
    supervisor = EdgeSupervisor(
        cluster_id="east-wing",
        db_path=db_file,
        slm_mode="mock",
    )

    now = datetime.now(timezone.utc)
    survey_lease = {
        "lease_id": "lease-task-survey-001-v1",
        "version": 1,
        "priority": "routine",
        "deadline_ts": (now + timedelta(minutes=60)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expiry_ts": (now + timedelta(minutes=65)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cluster_id": "east-wing",
        "assigned_robots": ["R1"],
        "allowed_zones": ["central-supply", "2B-12", "east_waiting_approach"],
        "forbidden_zones": ["ward-2C-isolation"],
        "speed_limit": 0.8,
        "task_sequence": [
            {"cmd": "go_to", "robot": "R1", "params": {"to": "central-supply"}},
            {"cmd": "photo", "robot": "R1", "params": {}},
            {"cmd": "report", "robot": "R1", "params": {}},
        ],
        "when_to_escalate": ["door_closed"],
    }

    res = supervisor.process_lease(survey_lease)
    assert res["status"] == "COMPLETED"
    assert "survey.json" in res["result_ref"]

    # Verify survey.json exists and has valid reports format
    survey_file = supervisor.artifact_packager.base_dir / "task-survey-001" / "survey.json"
    assert survey_file.exists()
    doc = json.loads(survey_file.read_text(encoding="utf-8"))
    assert "reports" in doc
    assert len(doc["reports"]) >= 1
    assert doc["reports"][0]["robot"] == "R1"
    assert "counts" in doc["reports"][0]


def test_outbox_selective_sharing_filter(tmp_path, mock_cloud_server):
    """
    PRD FR-11 & docs/p2-edge-contract-ms2.md Sec 3:
    fetch_leases_from_cloud must only return leases intended for its own cluster
    (e.g. east-wing), filtering out west-wing leases.
    """
    db_file = str(tmp_path / "test_selective.db")
    supervisor = EdgeSupervisor(
        cluster_id="east-wing",
        cloud_url=mock_cloud_server,
        db_path=db_file,
        slm_mode="mock",
    )

    leases = supervisor.fetch_leases_from_cloud()
    assert len(leases) == 1
    assert leases[0]["lease_id"] == "lease-task-100-v1"
    assert leases[0]["cluster_id"] == "east-wing"
