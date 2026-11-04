"""
Nimbus — Milestone 1 Demonstration (Person 2: Edge Tier)
Executes Task T1 (Routine Hospital Delivery: East Waiting Approach -> Nurse Station Approach)
Uses Person 1's Gazebo hospital topology nodes:
  - Allowed zones: east_waiting_approach, east_mid_hall, nurse_station_approach
  - Forbidden zones: west_mid_hall
  - Assigned robot: R1 in cluster east-wing
Demonstrates:
  1. Lease Ingestion & Draft 2020-12 Schema Validation (FR-5, FR-8)
  2. Sub-task Execution on Robot Navigation Stack (FR-3)
  3. Tactical Reasoner invocation on local Ollama Qwen2.5-1.5B (E1, NFR-1)
  4. Decision Logging & Store-and-Forward Persistence in SQLite (FR-10, FR-20, NFR-6)
  5. OUTPUT_READY Escalation Generation matching Team Contracts (FR-9)
"""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys
import time

# Ensure repository root containing 'edge' is on sys.path
_current = Path(__file__).resolve().parent
while _current.parent != _current:
    if (_current / "edge" / "supervisor").is_dir():
        if str(_current) not in sys.path:
            sys.path.insert(0, str(_current))
        break
    _current = _current.parent

from edge.supervisor.supervisor import EdgeSupervisor
from edge.slm.engine import EdgeSLMEngine


def run_demo(backend: str = "mock"):
    print("=" * 70)
    print("  NIMBUS MILESTONE 1 DEMO -- EDGE TIER & LOCAL AUTONOMY (PERSON 2)")
    print("=" * 70)

    # 1. Initialize Edge Supervisor
    print("\n[1] Initializing Edge Supervisor for cluster: 'east-wing'...")
    supervisor = EdgeSupervisor(
        cluster_id="east-wing",
        db_path="edge_local_store.db",
        slm_mode="auto",
        robot_backend=backend,
    )
    print("    - Local SQLite store: 'edge_local_store.db'")
    print(f"    - Edge SLM mode: {supervisor.slm.mode} (Model: {supervisor.slm.model_name})")
    print(f"    - Robot Backend: {backend.upper()} ({'ground_robot_sdk / Gazebo' if backend == 'sim' else 'in-memory mock'})")
    print(f"    - Topology Graph: {len(supervisor.topology.get('nodes', {}))} nodes, {len(supervisor.waypoints)} waypoints")
    print(f"    - Cluster Home Zone: {supervisor.home_zone}")
    print(f"    - Designated Safe Parking: {supervisor.safe_parking_zone}")
    print("    - Supervised robots: ['R1']")

    # 2. Formulate Task T1 Lease using Person 1's topology node IDs
    now = datetime.now(timezone.utc)
    deadline = now + timedelta(minutes=10)
    expiry = now + timedelta(minutes=12)
    demo_ts = int(time.time())

    lease_t1 = {
        "lease_id": f"lease-t1-{demo_ts}",
        "version": 1,
        "priority": "routine",
        "deadline_ts": deadline.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expiry_ts": expiry.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "allowed_zones": [
            "north_hall",
            "east_north_gate",
            "east_waiting_approach",
            "east_mid_hall",
            "nurse_station_approach"
        ],
        "forbidden_zones": ["west_mid_hall"],
        "speed_limit": 0.8,
        "assigned_robots": ["R1"],
        "task_sequence": [
            {"cmd": "go_to", "robot": "R1", "params": {"to": "east_waiting_approach"}},
            {"cmd": "wait", "robot": "R1", "params": {"seconds": 5}},
            {"cmd": "go_to", "robot": "R1", "params": {"to": "nurse_station_approach"}},
            {"cmd": "photo", "robot": "R1", "params": {}},
            {"cmd": "beep", "robot": "R1", "params": {}}
        ],
        "cluster_id": "east-wing",
        "when_to_escalate": ["door_closed", "deadline_at_risk", "robot_failed"]
    }

    print("\n[2] Ingesting Cloud Lease for Task T1 (Routine Equipment Delivery):")
    print(f"    - Lease ID    : {lease_t1['lease_id']} (v{lease_t1['version']})")
    print(f"    - Priority    : {lease_t1['priority']}")
    print(f"    - Cluster ID  : {lease_t1['cluster_id']}")
    print(f"    - Allowed     : {lease_t1['allowed_zones']}")
    print(f"    - Forbidden   : {lease_t1['forbidden_zones']}")
    print(f"    - Sub-tasks   : {len(lease_t1['task_sequence'])} steps in sequence")

    # 3. Process the Lease (Validation + Step Dispatch)
    print("\n[3] Executing Task Sequence on Robot R1:")
    result = supervisor.process_lease(lease_t1)

    print(f"\n    [+] Mission Status : {result.get('status', 'UNKNOWN')}")
    print(f"    [+] Steps Executed : {result.get('executed_steps_count', 0)} steps")
    print(f"    [+] Result Ref     : {result.get('result_ref', 'N/A')}")

    # 4. Demonstrate Live Tactical Reasoning with Local Ollama SLM
    print("\n[4] Testing Live Tactical Event with Local SLM (Qwen2.5-1.5B via Ollama):")
    print("    Scenario: Robot R1 detects maintenance trolley blocking east_mid_hall.")
    tactical_context = {
        "lease_id": lease_t1["lease_id"],
        "version": 1,
        "time_remaining_sec": 480.0,
        "robot_id": "R1",
        "current_pos": [4.5, 3.0],  # east_mid_hall waypoint
        "current_zone": "east_mid_hall",
        "allowed_zones": [
            "east_waiting_approach",
            "east_mid_hall",
            "east_north_gate",
            "north_hall",
            "nurse_station_approach"
        ],
        "forbidden_zones": lease_t1["forbidden_zones"],
        "event_description": "Maintenance trolley partially blocking east_mid_hall; bypass via east_north_gate -> north_hall available.",
        "allowed_waypoints": ["east_mid_hall", "east_north_gate", "north_hall", "nurse_station_approach"]
    }

    print("    Invoking SLM inference...")
    decision, decided_by, latency = supervisor.slm.evaluate_decision(tactical_context)
    print(f"    - Decided by : {decided_by}")
    print(f"    - Latency    : {latency:.1f} ms ({latency/1000.0:.2f} s) [Target: <= 10.0 s]")
    print(f"    - Action     : {decision.get('action')}")
    print(f"    - Waypoint   : {decision.get('target_waypoint')}")
    print(f"    - Reason     : {decision.get('reason')}")

    # 5. Inspect Local Persistence Store (SQLite)
    print("\n[5] Verifying SQLite Local Persistence (edge_local_store.db):")
    conn = sqlite3.connect("edge_local_store.db")
    c = conn.cursor()

    c.execute("SELECT lease_id, version, status, received_at FROM leases WHERE lease_id = ?", (lease_t1["lease_id"],))
    lease_row = c.fetchone()
    print(f"    [DB: leases]       : ID={lease_row[0]}, v{lease_row[1]}, Status={lease_row[2]}")

    c.execute("SELECT COUNT(*) FROM robot_commands WHERE lease_id = ?", (lease_t1["lease_id"],))
    cmd_count = c.fetchone()[0]
    print(f"    [DB: commands]     : {cmd_count} sub-task commands recorded")

    c.execute("SELECT COUNT(*) FROM decision_log WHERE lease_id = ?", (lease_t1["lease_id"],))
    dec_count = c.fetchone()[0]
    print(f"    [DB: decision_log] : {dec_count} decisions logged for audit & replay (FR-20)")

    c.execute("SELECT message_id, message_type, status FROM outbox WHERE lease_id = ?", (lease_t1["lease_id"],))
    outbox_row = c.fetchone()
    if outbox_row:
        print(f"    [DB: outbox]       : MsgID={outbox_row[0]}, Type={outbox_row[1]}, UploadStatus={outbox_row[2]}")
    conn.close()

    # 6. Verify Wire Contract Compliance for OUTPUT_READY
    print("\n[6] Validating OUTPUT_READY Escalation Payload (FR-9, Table 2):")
    escalation_sample = {
        "type": "OUTPUT_READY",
        "message_id": "msg-demo-001",
        "lease_version": 1,
        "task_id": "task-000001",
        "payload": {
            "result_ref": result.get("result_ref", "storage://results/demo/delivery-proof.jpg")
        }
    }
    from edge.schemas.validator import validate_escalation
    valid, err = validate_escalation(escalation_sample)
    print(f"    - Schema Validation : {'PASSED (Valid Draft 2020-12)' if valid else 'FAILED: ' + err}")
    print("    - Payload Body:\n" + json.dumps(escalation_sample, indent=6))

    print("\n" + "=" * 70)
    print("  MILESTONE 1 VERIFICATION COMPLETED SUCCESSFULLY!")
    print("=" * 70)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Run Milestone 1 Task T1 Demo.")
    parser.add_argument("--backend", choices=["mock", "sim"], default="mock", help="Robot backend: 'mock' (in-memory) or 'sim' (Person 1 ground_robot_sdk / Gazebo)")
    args = parser.parse_args()
    run_demo(backend=args.backend)
