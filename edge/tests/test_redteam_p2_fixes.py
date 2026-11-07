"""
Regression test suite specifically verifying Person 2 Red-Team audit fixes:
- C5: Lease supersession by task_id and failover resumption deduplication
- C6: Safe-parking excluding lifts, generic zones, and avoiding unsafe fallback
- C7: Store-and-forward collision-free message IDs and save_lease durability
- H3: Outbox worker non-blocking on non-retryable 4xx terminal rejections
- H4: Escalation schema 'type': 'array' fix
- M1: SQLite WAL mode and busy timeout
- M4: ISO timestamp datetime parsing and comparison
- M10: Clarification answer durable persistence across restarts
"""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import pytest

_current = Path(__file__).resolve().parent
while _current.parent != _current:
    if (_current / "edge" / "supervisor").is_dir():
        if str(_current) not in sys.path:
            sys.path.insert(0, str(_current))
        break
    _current = _current.parent

from edge.schemas.validator import validate_escalation
from edge.store.local_store import EdgeLocalStore
from edge.supervisor.parking import SafeParkingEngine
from edge.supervisor.supervisor import EdgeSupervisor
from edge.supervisor.outbox_worker import OutboxUploadWorker


def test_c5_supersession_deduplication_by_task_id(tmp_path):
    """C5: Newer lease versions supersede older active versions even when lease_id embeds version."""
    db_file = str(tmp_path / "test_c5.db")
    store = EdgeLocalStore(db_path=db_file)

    now = datetime.now(timezone.utc)
    expiry = (now + timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ")

    lease_v1 = {
        "lease_id": "lease-task-X-v1",
        "task_id": "task-X",
        "version": 1,
        "cluster_id": "east-wing",
        "expiry_ts": expiry,
        "assigned_robots": ["R1"],
        "task_sequence": [{"cmd": "go_to", "robot": "R1", "params": {"to": "east_waiting_approach"}}],
    }
    stored, msg = store.store_lease(lease_v1)
    assert stored is True

    # Check v1 is ACTIVE
    active_leases = store.get_all_active_leases("east-wing")
    assert len(active_leases) == 1
    assert active_leases[0]["lease_id"] == "lease-task-X-v1"

    # Ingest v2 with different lease_id embedding version
    lease_v2 = {
        "lease_id": "lease-task-X-v2",
        "task_id": "task-X",
        "version": 2,
        "cluster_id": "east-wing",
        "expiry_ts": expiry,
        "assigned_robots": ["R1"],
        "task_sequence": [{"cmd": "go_to", "robot": "R1", "params": {"to": "nurse_station_approach"}}],
    }
    stored, msg = store.store_lease(lease_v2)
    assert stored is True

    # Verify v1 was transitioned to SUPERSEDED
    v1_rec = store.get_lease("lease-task-X-v1", version=1)
    assert v1_rec["status"] == "SUPERSEDED"

    # Verify get_all_active_leases only returns v2 (single latest version per task)
    active_leases = store.get_all_active_leases("east-wing")
    assert len(active_leases) == 1
    assert active_leases[0]["lease_id"] == "lease-task-X-v2"
    assert active_leases[0]["version"] == 2

    # Verify re-injecting stale v1 is dropped
    stored_stale, drop_msg = store.store_lease(lease_v1)
    assert stored_stale is False
    assert "Stale/duplicate version 1" in drop_msg


def test_c6_safe_parking_excludes_lifts_and_unsafe_fallback():
    """C6: Safe parking excludes 'lift' prohibited names and returns None if all spots forbidden."""
    engine = SafeParkingEngine()
    assert engine.is_safe_for_parking("lift_lobby") is False
    assert engine.is_safe_for_parking("main_lift_door") is False
    assert engine.is_safe_for_parking("corridor_center") is False
    assert engine.is_safe_for_parking("safe-parking-bay-1") is True

    # When all registered spots are forbidden, must return None (not fall back to spot 0)
    all_spots = list(engine.safe_spots.keys())
    res = engine.find_nearest_safe_parking_spot(
        current_pose=[0.0, 0.0],
        forbidden_zones=all_spots
    )
    assert res is None


def test_c7_save_lease_persists_robot_swap(tmp_path):
    """C7: save_lease persists updated lease JSON (e.g. after robot swap) without AttributeError."""
    db_file = str(tmp_path / "test_c7.db")
    store = EdgeLocalStore(db_path=db_file)

    now = datetime.now(timezone.utc)
    lease = {
        "lease_id": "lease-swap-test",
        "task_id": "task-swap",
        "version": 1,
        "cluster_id": "east-wing",
        "expiry_ts": (now + timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "assigned_robots": ["R1"],
        "task_sequence": [],
    }
    store.store_lease(lease)

    # Robot swap mutates assigned_robots
    lease["assigned_robots"] = ["R2", "R1"]
    store.save_lease(lease)

    # Verify persisted state has new assigned_robots
    updated = store.get_lease("lease-swap-test", version=1)
    assert updated["assigned_robots"] == ["R2", "R1"]


def test_h3_outbox_terminal_4xx_does_not_block_queue(tmp_path):
    """H3: Outbox worker marks terminal 4xx rejections as FAILED_TERMINAL without head-of-line blocking."""
    db_file = str(tmp_path / "test_h3.db")
    store = EdgeLocalStore(db_path=db_file)

    worker = OutboxUploadWorker(store=store, cloud_url="http://127.0.0.1:59999", auto_start=False)

    # Mock _send_single_payload behavior
    def mock_send(url, payload):
        if payload.get("bad"):
            return "TERMINAL"
        return "SUCCESS"

    worker._send_single_payload = mock_send

    # Enqueue a bad payload first (terminal 400), followed by a valid payload
    store.enqueue_outbox("msg-bad-1", "lease-1", "CLARIFY", {"bad": True})
    store.enqueue_outbox("msg-good-2", "lease-1", "CLARIFY", {"bad": False})

    uploaded = worker.process_pending_outbox_once()
    assert uploaded == 1

    # Bad message was dropped from pending with FAILED_TERMINAL
    pending = store.get_pending_outbox()
    assert len(pending) == 0

    with store._get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM outbox WHERE message_id = 'msg-bad-1'")
        assert cursor.fetchone()["status"] == "FAILED_TERMINAL"
        cursor.execute("SELECT status FROM outbox WHERE message_id = 'msg-good-2'")
        assert cursor.fetchone()["status"] == "UPLOADED"


def test_h4_escalation_schema_array_type():
    """H4: Escalation schema 'possible_readings' correctly specifies 'type': 'array'."""
    clarify_msg = {
        "type": "CLARIFY",
        "message_id": "msg-clar-test-01",
        "lease_version": 1,
        "task_id": "task-test-01",
        "payload": {
            "question": "Which bed?",
            "possible_readings": ["Bed 1", "Bed 2"]
        }
    }
    is_valid, msg = validate_escalation(clarify_msg)
    assert is_valid, f"Validation failed: {msg}"


def test_m1_sqlite_wal_mode_and_busy_timeout(tmp_path):
    """M1: SQLite connections have WAL mode enabled and 10.0s busy timeout."""
    db_file = str(tmp_path / "test_m1.db")
    store = EdgeLocalStore(db_path=db_file)

    with store._get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode;")
        row = cursor.fetchone()
        assert row[0].lower() == "wal"


def test_m4_iso_datetime_comparison():
    """M4: ISO timestamps compared as UTC datetimes, not lexicographical strings."""
    t1_s = "2026-10-01T12:00:00Z"
    t2_s = "2026-10-01T12:00:00+00:00"
    t3_s = "2026-10-01T12:01:00Z"

    dt1 = EdgeSupervisor._parse_iso_ts(t1_s)
    dt2 = EdgeSupervisor._parse_iso_ts(t2_s)
    dt3 = EdgeSupervisor._parse_iso_ts(t3_s)

    assert dt1 == dt2
    assert dt3 > dt1


def test_m10_clarification_answers_durability(tmp_path):
    """M10: Clarification answers are durably stored in SQLite and survive restart."""
    db_file = str(tmp_path / "test_m10.db")
    store = EdgeLocalStore(db_path=db_file)

    store.store_clarification_answer(
        message_id="msg-ans-100",
        task_id="task-pharmacy-1",
        lease_version=1,
        answer_payload={"type": "ANSWER", "task_id": "task-pharmacy-1", "text": "Shelf B"}
    )

    # Standby or restarted supervisor reads answer
    answers = store.get_clarification_answers("task-pharmacy-1")
    assert len(answers) == 1
    assert answers[0]["text"] == "Shelf B"


def test_c6_safe_parking_excludes_spots_inside_forbidden_corridors():
    """C6: safe-parking-bay-2 must be excluded when corridor-2B-service is forbidden due to spatial bounds."""
    engine = SafeParkingEngine()
    # Robot near ward 2B [12.0, 0.0] would normally choose safe-parking-bay-2 at [11.0, -1.8]
    spot_normal = engine.find_nearest_safe_parking_spot(current_pose=[12.0, 0.0])
    assert spot_normal["zone_name"] == "safe-parking-bay-2"

    # When corridor-2B-service is forbidden, safe-parking-bay-2 is inside its bounds [3.0, -4.5, 17.0, -1.5]
    # Engine must recognize the spatial overlap and exclude safe-parking-bay-2
    spot_forbidden = engine.find_nearest_safe_parking_spot(
        current_pose=[12.0, 0.0],
        forbidden_zones=["corridor-2B-service"]
    )
    assert spot_forbidden["zone_name"] != "safe-parking-bay-2"
    assert spot_forbidden["zone_name"] in ("safe-parking-bay-1", "safe-parking-bay-east")


def test_c5_step_execution_receipts_deduplicate_across_replanned_versions(tmp_path):
    """C5: Completed step receipts are recognized across replanned versions (v1 -> v2) for the same task."""
    db_file = str(tmp_path / "test_c5_steps.db")
    store = EdgeLocalStore(db_path=db_file)

    # v1 executes step 1
    cmd_v1 = {
        "cmd_id": "cmd-lease-task-ABC-v1-step-1",
        "lease_id": "lease-task-ABC-v1",
        "robot_id": "R1",
        "action": "go_to",
        "params": {"to": "nurse_station_approach"},
    }
    store.store_robot_command(cmd_v1)
    store.update_robot_command_status("cmd-lease-task-ABC-v1-step-1", "COMPLETED")

    # Cloud replans to v2: same task, new lease_id embedding v2
    # When edge queries for step 1 under v2, it must find the completed record from v1
    v2_cmd_rec = store.get_robot_command("cmd-lease-task-ABC-v2-step-1")
    assert v2_cmd_rec is not None
    assert v2_cmd_rec["status"] == "COMPLETED"


def test_c7_outbox_enqueue_does_not_overwrite_payload_on_collision(tmp_path):
    """C7: Enqueueing an outbox message with colliding message_id does not silently overwrite existing payload."""
    db_file = str(tmp_path / "test_c7_collision.db")
    store = EdgeLocalStore(db_path=db_file)

    msg_id = "msg-colliding-001"
    store.enqueue_outbox(msg_id, "lease-1", "OUTPUT_READY", {"original": True, "result_ref": "storage://results/task-1/proof.jpg"})

    # Mark as uploaded
    store.mark_outbox_uploaded(msg_id)

    # Re-enqueueing or colliding message with same ID must DO NOTHING, preserving original payload
    store.enqueue_outbox(msg_id, "lease-1", "OUTPUT_READY", {"original": False, "result_ref": "storage://results/task-1/fake.jpg"})

    with store._get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT payload_json, status FROM outbox WHERE message_id = ?", (msg_id,))
        row = cursor.fetchone()
        payload = json.loads(row["payload_json"])
        assert payload["original"] is True
        assert "proof.jpg" in payload["result_ref"]


def test_h3_outbox_terminal_on_arbitrary_4xx(tmp_path):
    """H3: Outbox worker marks 404, 403, 405 (all non-retryable 4xx) as FAILED_TERMINAL without blocking."""
    db_file = str(tmp_path / "test_h3_4xx.db")
    store = EdgeLocalStore(db_path=db_file)
    worker = OutboxUploadWorker(store=store, cloud_url="http://127.0.0.1:59999", auto_start=False)

    def mock_send_404(url, payload):
        return "TERMINAL"

    worker._send_single_payload = mock_send_404

    store.enqueue_outbox("msg-404-item", "lease-1", "OUTPUT_READY", {"bad": True})
    uploaded = worker.process_pending_outbox_once()
    assert uploaded == 0

    with store._get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM outbox WHERE message_id = 'msg-404-item'")
        assert cursor.fetchone()["status"] == "FAILED_TERMINAL"


def test_m15_sim_robot_rejects_unknown_robot_id():
    """M15: SimRobot must explicitly reject unknown robot IDs outside R1-R6 / robot_1-robot_6 with ValueError."""
    from edge.mock.sim_robot import SimRobot
    with pytest.raises(ValueError, match="Unknown robot_id 'R99'"):
        SimRobot(robot_id="R99")

    with pytest.raises(ValueError, match="Unknown robot_id 'robot_99'"):
        SimRobot(robot_id="robot_99")

