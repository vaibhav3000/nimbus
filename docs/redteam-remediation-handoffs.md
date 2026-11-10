# Cross-Team Red-Team Remediation Handoffs (P1, P2, P3)

**Document Purpose:** Formal cross-team handoff register for Project Nimbus findings owned by P1 (Simulation/World), P2 (Edge Robotics/Safety), and P3 (Cloud Orchestrator).  
**Author:** Person 4 (Cloud Infrastructure & Deployment)  
**Rule:** P4 has implemented ingress protection, schema validation, and durable persistence wrappers on the cloud side, but has not modified internal P1, P2, or P3 codebases. This document provides exact locations, reproduction commands, and acceptance criteria for each owner.

---

## 1. Cross-Team Blocker Summary Matrix

| Finding ID | Finding Title | Owning Team | Severity | Status | Cloud (P4) Integration Status |
| :--- | :--- | :---: | :---: | :---: | :--- |
| **HO-P1-01** | Safe Parking Node Geofence Discrepancy | **P1** | High | Confirmed Open | Pack validation enabled in `contracts.py` |
| **HO-P1-02** | Gazebo vs Kinematic World Topology Drift | **P1** | Medium | Confirmed Open | Default pack pinned to `hospital_sim6` |
| **HO-P2-01** | Outbox Message Overwrite on Identical Task Key | **P2** | High | Confirmed Open | In-flight deduplication table added to cloud ingress |
| **HO-P2-02** | Edge SQLite Connection Leak Under High Check-in Rate | **P2** | High | Confirmed Open | Bounded connection timeout in `mqtt_transport.py` |
| **HO-P2-03** | Lease Supersession Loss After Edge Supervisor Restart | **P2** | Critical | Confirmed Open | Cloud preserves monotonic lease counter in DynamoDB |
| **HO-P2-04** | OUTPUT_READY UUID Duplication Across Robot Legs | **P2** | High | Confirmed Open | Idempotent `OUTPUT_READY` return in `server.py` |
| **HO-P3-01** | Task Completion vs Operator Cancellation Race Condition | **P3** | High | Confirmed Open | `_STATE_LOCK` wraps control & completion in `server.py` |
| **HO-P3-02** | Decision-Log Causal Ordering Inversion on Edge Replay | **P3** | High | Confirmed Open | Cloud monotonic version stamping on decision append |
| **HO-P3-03** | Missing Application EventBridge Publisher | **P3** | Medium | Confirmed Open | Terraform bus provisioned; handoff to P3 publisher |

---

## 2. P1 Handoffs: Simulation and World Topology

### HO-P1-01: Safe Parking Node Geofence Discrepancy
- **Severity:** High
- **Owning Team:** Person 1 (Simulation & World)
- **Location:** `packs/hospital_sim6/pack.json`, `packs/hospital_sim6/nodes.json`
- **Evidence:** In `hospital_sim6`, parking nodes for `R2` and `R3` overlap dynamic delivery corridor coordinates in the Gazebo mesh. If an area closure policy (`SOP-ISO-02`) closes the corridor, robots instructed to park enter forbidden zones.
- **Reproduction Steps:**
  1. Set corridor closure: `POST /map/reopen` with closed nodes `["corr_east_1"]`.
  2. Dispatch task `task-001` to `R2`.
  3. Cancel task via `POST /tasks/task-001/control` (`action: "cancel"`).
  4. Inspect robot final coordinate: robot stops inside forbidden node `corr_east_1`.
- **Expected Behavior:** Robots must park exclusively at designated standby bays outside active transit corridors.
- **Required Fix:** Move parking waypoint coordinates for `R2` and `R3` in `packs/hospital_sim6/nodes.json` to dedicated non-transit bays.
- **P4 Integration Completed:** Cloud allocator verifies geofence constraints and generates zero-speed stop leases; allocator fails closed if standby node is enclosed in forbidden zone.
- **Acceptance Criterion:** Geofenced nodes validation test in `tests/test_allocator.py` passes without zone boundary violations.

---

## 3. P2 Handoffs: Edge Execution and Safety

### HO-P2-01: Outbox Message Overwrite on Identical Task Key
- **Severity:** High
- **Owning Team:** Person 2 (Edge Supervisor)
- **Location:** `edge/supervisor.py:outbox_insert()`
- **Evidence:** Edge SQLite table uses `INSERT OR REPLACE INTO outbox (task_id, payload)` rather than keying by unique `message_id`. If an edge robot generates multiple events for the same task in rapid succession (e.g., `UNEXPECTED_EVENT` followed by `OUTPUT_READY`), the second event overwrites the first before the first is transmitted to cloud.
- **Reproduction Steps:**
  1. Trigger edge obstacle event: robot emits `UNEXPECTED_EVENT`.
  2. Immediately trigger robot arrival: robot emits `OUTPUT_READY`.
  3. Query edge local SQLite outbox: only `OUTPUT_READY` row exists; `UNEXPECTED_EVENT` was discarded.
- **Expected Behavior:** Every discrete edge message must have a unique primary key `message_id` with append-only queue semantics.
- **Required Fix:** Change edge SQLite schema to `PRIMARY KEY (message_id)` and use `INSERT INTO outbox`.
- **P4 Integration Completed:** `server.py:_dedupe` and `_IN_FLIGHT_MESSAGES` correctly handle separate message IDs and deduplicate retransmissions without dropping distinct events.
- **Acceptance Criterion:** Multi-event trace replay in `tests/test_fault_harness.py` records both `UNEXPECTED_EVENT` and `OUTPUT_READY` in the cloud decision log.

### HO-P2-03: Lease Supersession Loss After Edge Supervisor Restart
- **Severity:** Critical
- **Owning Team:** Person 2 (Edge Supervisor)
- **Location:** `edge/run_supervisor.py`
- **Evidence:** When an edge supervisor crashes or restarts during a network outage, local volatile memory resets active lease version to 0. If cloud emits lease version 1, edge supervisor accepts it, but if cloud previously emitted lease version 2 before the reboot, the edge supervisor may execute a replayed version 1 lease.
- **Reproduction Steps:**
  1. Issue lease version 2 to edge cluster `east-wing`.
  2. Kill supervisor process (`SIGKILL`).
  3. Replay network packet carrying lease version 1.
  4. Supervisor accepts stale lease version 1 because restart cleared memory cache.
- **Expected Behavior:** Supervisor must persist active lease version to local SQLite or non-volatile storage and reject any lease version $\le V_{\text{persisted}}$.
- **Required Fix:** Store latest `lease_version` in edge SQLite DB and reload on process initialization.
- **P4 Integration Completed:** Cloud strictly increments monotonic version numbers in DynamoDB and tags all leases with version stamps.
- **Acceptance Criterion:** Test in `tests/test_cloud_api.py::test_checkin_with_stale_version_is_told_the_current_one` continues to pass with local persistence enabled.

---

## 4. P3 Handoffs: Cloud Orchestrator Internals

### HO-P3-01: Task Completion vs Operator Cancellation Race Condition
- **Severity:** High
- **Owning Team:** Person 3 (Cloud Orchestrator)
- **Location:** `cloud/orchestrator.py:finish()` vs `cloud/orchestrator.py:cancel()`
- **Evidence:** If an operator cancels a task (`POST /tasks/{id}/control`, `action: "cancel"`) at the exact millisecond that edge emits `OUTPUT_READY`, race conditions in `cloud/orchestrator.py` can cause inventory stock to be double-incremented (once by cancel return, once by finish restock).
- **Reproduction Steps:**
  1. Lease task for item `wheelchair` (stock drops from 5 to 4).
  2. Concurrently execute `orch.cancel(task)` and `orch.finish(task)` across two worker threads without lock.
  3. Inspect final stock count: stock increments to 6 (phantom inventory created).
- **Expected Behavior:** Mutually exclusive state transitions: a task is either `cancelled` or `completed`, never both.
- **Required Fix:** Add explicit state transition check inside `cloud/orchestrator.py` methods: `if task["status"] in TERMINAL_STATUSES: return`.
- **P4 Integration Completed:** `server.py` wraps `_post_control` and `process_escalation` in `_STATE_LOCK` and rejects `OUTPUT_READY` on already-completed or cancelled tasks with `status: "already_completed", duplicate: True`.
- **Acceptance Criterion:** Concurrent test `tests/test_p4_security.py::test_idempotent_output_ready_completion` passes and inventory stock remains strictly conserved.

### HO-P3-03: Missing Application EventBridge Publisher
- **Severity:** Medium
- **Owning Team:** Person 3 (Cloud Orchestrator)
- **Location:** `cloud/orchestrator.py`
- **Evidence:** Terraform provisions an Amazon EventBridge rule for task status events, but `cloud/orchestrator.py` does not contain a publisher calling `boto3.client("events").put_events`.
- **Reproduction Steps:**
  1. Create and complete a task.
  2. Inspect CloudWatch EventBridge invocation metrics: metric count is 0.
- **Expected Behavior:** Orchestrator should publish an event `NimbusTaskTransition` to the default bus on lease creation, cancellation, and completion.
- **Required Fix:** Introduce an optional EventBridge publisher in `cloud/orchestrator.py` wrapped in try/except so AWS-less tests remain green.
- **P4 Integration Completed:** Terraform infrastructure provisioned in `infra/terraform/main.tf` with correct IAM permissions.
- **Acceptance Criterion:** CloudWatch event rule target triggers when a task is completed in live AWS environment.
