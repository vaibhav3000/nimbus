# Cross-Team Red-Team Remediation Handoffs (P1, P2, P3)

**Document Purpose:** Formal cross-team handoff register for Project Nimbus findings owned by P1 (Simulation/World), P2 (Edge Robotics/Safety), and P3 (Cloud Orchestrator), compiled during P4 final red-team verification.  
**Author:** Person 4 (Cloud Infrastructure & Deployment)  
**Rule:** P4 has implemented ingress protection, schema validation, durable persistence wrappers, and infrastructure provisions on the cloud side, but has strictly preserved internal P1, P2, and P3 codebases. This document provides exact source locations, reproduction steps, required fixes, P4 integration completed, remaining dependencies, and acceptance criteria for each owner.

---

## 1. Master Handoff Summary Matrix

| Finding ID | Finding Title | Owning Team | Severity | P4 Integration Status | Remaining Dependency |
| :--- | :--- | :---: | :---: | :--- | :--- |
| **HO-P1-01** | Web Viewer Unescaped HTML Rendering (XSS Risk) | **P1** | High | P4 hardened server headers (`Content-Security-Policy`) | P1 sanitize viewer template variables |
| **HO-P1-02** | Robot Identity Naming Inconsistency across Sim | **P1** | Medium | Contracts schema validates canonical `R[1-9]` ids | P1 map Gazebo robot model names to canonical ids |
| **HO-P1-03** | Safe Parking Waypoint Geofence Discrepancy | **P1** | High | P4 allocator checks geofences and returns stop leases | P1 move standby bay waypoints outside corridors |
| **HO-P1-04** | Gazebo Mesh vs Kinematic 2D World Coordinate Drift | **P1** | Medium | Mapview endpoint normalizes coordinates within bounds | P1 align world frame transforms in SDF mesh |
| **HO-P2-01** | Outbox Message Overwrite on Identical Task Key | **P2** | High | P4 dedupes incoming stream by `message_id` | P2 change SQLite primary key to `message_id` |
| **HO-P2-02** | Lease Reconcile Magic Version `999` Fallback | **P2** | Medium | P4 rejects non-monotonic versions with HTTP 409 | P2 remove hard-coded `999` reconcile fallback |
| **HO-P2-03** | Lease Supersession Loss After Edge Supervisor Restart | **P2** | Critical | Cloud persists monotonic version counter | P2 persist highest known version in SQLite |
| **HO-P2-04** | Missing `save_lease` Method Call Interface Mismatch | **P2** | High | Lease JSON contract validated in transport | P2 reconcile `save_lease` vs `persist_lease` method |
| **HO-P2-05** | OUTPUT_READY UUID Duplication Across Multi-Leg Tasks | **P2** | High | Server treats repeated OUTPUT_READY as idempotent | P2 generate distinct subtask completion IDs |
| **HO-P2-06** | Poison-Message Infinite Retry Without Dead-Letter | **P2** | Medium | P4 client supports bounded retries | P2 add poison message counter and quarantine queue |
| **HO-P2-07** | MQTT Recovery Incomplete in Headless Edge Client | **P2** | High | P4 implemented buffered client with auto-reconnect | P2 integrate buffered transport into supervisor loop |
| **HO-P2-08** | Artifact Delivery Reference URI Mismatch (`storage://` vs `s3://`)| **P2/P3** | High | Cloud `ResultStore` configured for S3 and local paths | P2 upload artifact bytes to S3; P3 resolve URI |
| **HO-P2-09** | CLARIFY Answer Persistence on Edge Local Store | **P2** | Medium | Server `/requests/{id}/answer` persists to cloud log | P2 resume suspended state on answer receipt |
| **HO-P2-10** | Edge SQLite Thread Contention Under High Event Rate | **P2** | Medium | P4 throttles telemetry dispatch | P2 configure WAL mode and retry timeout in SQLite |
| **HO-P3-01** | Task Completion vs Operator Cancellation Race Condition | **P3** | High | P4 wrapped state transitions in `_STATE_LOCK` | P3 check `TERMINAL_STATUSES` before restock |
| **HO-P3-02** | Decision-Log Causal Inversion on Edge Replay | **P3** | High | Monotonic sequence stamping in cloud store | P3 enforce parent-child causal order in log stream |
| **HO-P3-03** | Bedrock Call Usage Token Accounting Missing in LLM Client | **P3** | Medium | P4 CostBook records estimated tokens | P3 populate `tokens_in` and `tokens_out` from Bedrock API |
| **HO-P3-04** | Shared State Lock Held During Synchronous LLM Calls | **P3** | High | Server releases lock during LLM decision slice | P3 ensure async model call cannot block ticker |
| **HO-P3-05** | SQS Visibility Timeout Lower Than Longest Replanning Slice | **P3** | Medium | Terraform visibility timeout increased to 300s | P3 heartbeat SQS message during multi-turn planning |
| **HO-P3-06** | Missing Application EventBridge Notification Publisher | **P3** | Low | EventBridge bus and rules provisioned in Terraform | P3 invoke `boto3.client('events').put_events` |

---

## 2. P1 Handoffs: Simulation and World Topology

### HO-P1-01: Web Viewer Unescaped HTML Rendering (XSS Risk)
- **Severity:** High
- **Owning Team:** Person 1 (Simulation & World)
- **Location:** `sim/viewer.py`, `static/map.html`
- **Evidence:** User-supplied task parameters (e.g. requester name, item labels) are interpolated directly into DOM elements without sanitization, exposing the web console to script injection.
- **Reproduction:** Submit request with text `Bed 2C <script>alert(1)</script>`. Inspect viewer DOM; raw `<script>` tag is rendered.
- **Required Fix:** Use `textContent` instead of `innerHTML`, or pass all variables through standard HTML escaping.
- **P4 Integration Completed:** Hardened HTTP response headers in `server.py` with `Content-Security-Policy: default-src 'self'`.
- **Remaining Dependency:** P1 to replace direct HTML string concatenations in simulation viewer templates.
- **Acceptance Criterion:** Submitting payload with HTML tags renders literal text without script execution.

### HO-P1-02: Robot Identity Naming Inconsistency Across Fleet and Simulation
- **Severity:** Medium
- **Owning Team:** Person 1 (Simulation & World)
- **Location:** `sim/robot.py`, `packs/hospital_sim6/fleet.json`
- **Evidence:** Simulation models use `robot_01`, `robot_02`, while the fleet registry and lease contracts require `R1`, `R2`, `R3`.
- **Reproduction:** Run `pytest tests/test_sim_robot.py`; test fails when comparing simulation entity names against contract schema.
- **Required Fix:** Standardize simulation entity names to canonical `R1`..`R6` format.
- **P4 Integration Completed:** `contracts/robot_subtask.schema.json` and `contracts/lease.schema.json` enforce canonical regex `^R[1-9][0-9]*$`.
- **Remaining Dependency:** P1 to update simulation spawn names to match fleet JSON.
- **Acceptance Criterion:** Simulation telemetries report robot IDs matching `contracts/fleet.schema.json`.

### HO-P1-03: Safe Parking Waypoint Geofence Discrepancy
- **Severity:** High
- **Owning Team:** Person 1 (Simulation & World)
- **Location:** `packs/hospital_sim6/pack.json`, `packs/hospital_sim6/topology.json`
- **Evidence:** In `hospital_sim6`, parking standby nodes for `R2` and `R3` overlap dynamic transit corridor coordinates. When quarantine closure `SOP-ISO-02` shuts the corridor, robots instructed to park remain inside the forbidden zone.
- **Reproduction:** Set corridor closure via `/map/reopen`. Cancel task assigned to `R2`. Robot stops inside closed corridor.
- **Required Fix:** Move parking waypoint coordinates for `R2` and `R3` to dedicated non-transit bays outside corridors.
- **P4 Integration Completed:** Cloud allocator verifies geofence constraints and generates zero-speed stop leases; allocator fails closed if standby node is enclosed in forbidden zone.
- **Remaining Dependency:** P1 topology coordinates updated.
- **Acceptance Criterion:** `tests/test_allocator.py` verifies all parking nodes remain accessible during corridor closures.

### HO-P1-04: Gazebo Mesh vs Kinematic 2D World Coordinate Drift
- **Severity:** Medium
- **Owning Team:** Person 1 (Simulation & World)
- **Location:** `sim/mapview.py`, `sim/gazebo/world.sdf`
- **Evidence:** 2D kinematic topology representation has an offset of 1.4m on the Y-axis relative to the 3D Gazebo collision mesh, causing 20 tests in `tests/test_mapview.py` to fail coordinate bounds assertions.
- **Reproduction:** Run `pytest tests/test_mapview.py`; 20 tests fail on boundary coordinate checks.
- **Required Fix:** Re-align world frame origin in Gazebo SDF world to match topological 2D grid origin (0,0).
- **P4 Integration Completed:** Cloud mapview handler in `server.py` clamps coordinates to bounding box.
- **Remaining Dependency:** P1 world SDF coordinate transform updated.
- **Acceptance Criterion:** `pytest tests/test_mapview.py` passes 100%.

---

## 3. P2 Handoffs: Edge Execution, Safety, and Artifacts

### HO-P2-01: Outbox Message Overwrite on Identical Task Key
- **Severity:** High
- **Owning Team:** Person 2 (Edge Supervisor)
- **Location:** `edge/supervisor.py:outbox_insert()`
- **Evidence:** Edge SQLite table uses `INSERT OR REPLACE INTO outbox (task_id, payload)` rather than keying by `message_id`. Rapid sequential events (`UNEXPECTED_EVENT` then `OUTPUT_READY`) overwrite the first event before transmission.
- **Reproduction:** Trigger obstacle event immediately followed by arrival. SQLite outbox contains only `OUTPUT_READY`; `UNEXPECTED_EVENT` is dropped.
- **Required Fix:** Define `PRIMARY KEY (message_id)` in edge SQLite outbox schema.
- **P4 Integration Completed:** Cloud ingress (`server.py:_dedupe`) handles arbitrary message sequences with in-flight reservation.
- **Remaining Dependency:** P2 schema migration to unique `message_id`.
- **Acceptance Criterion:** Both `UNEXPECTED_EVENT` and `OUTPUT_READY` reach the cloud decision log.

### HO-P2-02: Lease Reconcile Magic Version `999` Fallback
- **Severity:** Medium
- **Owning Team:** Person 2 (Edge Supervisor)
- **Location:** `edge/reconcile.py`
- **Evidence:** Reconcile routine falls back to version `999` during unknown version states, causing lease version jumps that break cloud monotonic sequencing.
- **Reproduction:** Send lease reconcile request with unknown version; observe edge requesting version 999.
- **Required Fix:** Reconcile must query cloud for `current_version` rather than transmitting hard-coded 999.
- **P4 Integration Completed:** `server.py` rejects non-monotonic version requests with HTTP 409 Conflict.
- **Remaining Dependency:** P2 reconcile logic updated.
- **Acceptance Criterion:** Version counter increments strictly monotonically without jumps.

### HO-P2-03: Lease Supersession Loss After Edge Supervisor Restart
- **Severity:** Critical
- **Owning Team:** Person 2 (Edge Supervisor)
- **Location:** `edge/run_supervisor.py`
- **Evidence:** Volatile memory resets active lease version to 0 on supervisor restart. Stale replayed lease versions from network can be erroneously accepted.
- **Reproduction:** Issue lease v2. Kill supervisor process (`SIGKILL`). Replay packet with lease v1. Supervisor accepts stale v1.
- **Required Fix:** Persist highest received `lease_version` in non-volatile SQLite DB; reject any incoming lease $\le V_{\text{persisted}}$.
- **P4 Integration Completed:** DynamoDB lease repository preserves monotonic lease counters across cloud instances.
- **Remaining Dependency:** P2 edge local state persistence.
- **Acceptance Criterion:** Replayed v1 lease is rejected by edge supervisor after process reboot.

### HO-P2-04: Missing `save_lease` Method Call Interface Mismatch
- **Severity:** High
- **Owning Team:** Person 2 (Edge Supervisor)
- **Location:** `edge/storage.py` vs `edge/supervisor.py`
- **Evidence:** `supervisor.py` calls `storage.save_lease(lease)`, but `storage.py` implements `persist_lease(lease)`, raising an `AttributeError` on edge lease save.
- **Reproduction:** Trigger edge lease reception; inspect supervisor error log.
- **Required Fix:** Add alias `save_lease = persist_lease` in `edge/storage.py`.
- **P4 Integration Completed:** Lease contract schema validated in `contracts.py`.
- **Remaining Dependency:** P2 method name reconciliation.
- **Acceptance Criterion:** Edge stores incoming lease without `AttributeError`.

### HO-P2-05: OUTPUT_READY UUID Duplication Across Multi-Leg Tasks
- **Severity:** High
- **Owning Team:** Person 2 (Edge Supervisor)
- **Location:** `edge/subtask.py`
- **Evidence:** Multi-leg handover tasks re-use the parent task's `task_id` for leg completions, causing subsequent leg `OUTPUT_READY` events to be dropped by deduplication.
- **Reproduction:** Execute multi-leg delivery task; second leg `OUTPUT_READY` receives duplicate drop acknowledgement.
- **Required Fix:** Suffix subtask outputs with leg index (e.g. `{task_id}#leg{n}`).
- **P4 Integration Completed:** `server.py` implements idempotent acceptance for already-completed tasks.
- **Remaining Dependency:** P2 leg subtask naming.
- **Acceptance Criterion:** All leg completions are acknowledged and recorded in decision log.

### HO-P2-06: Poison-Message Infinite Retry Without Dead-Letter Handling
- **Severity:** Medium
- **Owning Team:** Person 2 (Edge Supervisor)
- **Location:** `edge/transport.py`
- **Evidence:** Malformed payloads cause edge processing loop to crash, reload, and re-read the identical payload indefinitely without quarantine.
- **Reproduction:** Inject malformed JSON to edge input socket; supervisor enters crash loop.
- **Required Fix:** Add retry counter; move message to `quarantine.json` after 3 failed parsing attempts.
- **P4 Integration Completed:** Cloud SQS queue configured with RedrivePolicy (maxReceiveCount=3) and dead-letter queue.
- **Remaining Dependency:** P2 local dead-letter queue.
- **Acceptance Criterion:** Edge isolates malformed payload after 3 retries and resumes normal operation.

### HO-P2-07: MQTT Recovery Incomplete in Headless Edge Client
- **Severity:** High
- **Owning Team:** Person 2 (Edge Supervisor)
- **Location:** `edge/mqtt_client.py`
- **Evidence:** When Mosquitto broker stops, edge client sets an internal disconnected flag but lacks a background reconnect worker and ring buffer, causing messages to be silently lost during WAN outages.
- **Reproduction:** Stop Mosquitto broker. Send 5 edge events. Restart Mosquitto. Events are missing.
- **Required Fix:** Implement ring-buffered pending message queue and automatic subscription restoration.
- **P4 Integration Completed:** P4 implemented full buffered reconnect client in `mqtt_transport.py` (`PahoMQTTClient` and `StandinMQTTClient`).
- **Remaining Dependency:** P2 edge supervisor to import and utilize `mqtt_transport.py`.
- **Acceptance Criterion:** Outage recovery test confirms zero lost messages after broker restart.

### HO-P2-08: Artifact Delivery Reference URI Mismatch (`storage://` vs `s3://`) (Section 9)
- **Severity:** High
- **Owning Team:** Person 2 (Edge) & Person 3 (Cloud)
- **Location:** `edge/result_builder.py` vs `cloud/results.py`
- **Evidence:** Edge generates local output files under `data/results/{task_id}/...` and publishes `OUTPUT_READY` with URI `storage://results/{task_id}/result.json`. However, edge NEVER performs an actual S3 PutObject upload! Meanwhile, in AWS production mode (`NIMBUS_BACKEND=aws`), Cloud `ResultStore` expects `s3://` URIs and fails with `ValueError: cannot resolve local storage path in aws backend`.
- **Reproduction:**
  1. Set `NIMBUS_BACKEND=aws` and `NIMBUS_S3_BUCKET=nimbus-outputs-930124786284`.
  2. Send `POST /escalations` with `type: "OUTPUT_READY"` and `result_ref: "storage://results/task-1/photo.jpg"`.
  3. Resolver raises `ValueError: cannot resolve local storage path in aws backend`.
- **Required Fix:**
  - P2 (Edge): Implement S3 upload utility `edge/uploader.py` using `boto3` or signed URLs to upload local artifact files to `s3://{bucket}/{task_id}/{filename}` before emitting `OUTPUT_READY`.
  - P3 (Cloud): In `cloud/results.py`, add fallback resolver that maps `storage://results/{task_id}/...` to S3 bucket keys or local paths when running in hybrid testbeds.
- **P4 Integration Completed:** S3 output bucket `nimbus-outputs-930124786284` and IAM execution policies provisioned in Terraform (`infra/terraform/main.tf`).
- **Remaining Dependency:** P2 artifact S3 upload script and P3 URI resolution handoff.
- **Acceptance Criterion:** Edge uploads artifact to S3; cloud retrieves and serves artifact via `GET /tasks/{id}/result`.

### HO-P2-09: CLARIFY Answer Persistence on Edge Local Store
- **Severity:** Medium
- **Owning Team:** Person 2 (Edge Supervisor)
- **Location:** `edge/clarify_handler.py`
- **Evidence:** When requester answers a clarification, edge receives the answer via `notice` topic but does not persist the answer to local task state, causing subsequent route execution to re-request clarification after edge restart.
- **Reproduction:** Trigger clarification; answer via `/requests/{id}/answer`; restart edge supervisor; edge prompts for clarification again.
- **Required Fix:** Persist answered clarification payload into edge SQLite `task_context` table.
- **P4 Integration Completed:** Server logs clarification and answer in decision log and broadcasts notice to cluster.
- **Remaining Dependency:** P2 local storage update.
- **Acceptance Criterion:** Edge resumes execution immediately following answer without re-asking.

### HO-P2-10: Edge SQLite Thread Contention Under High Event Rate
- **Severity:** Medium
- **Owning Team:** Person 2 (Edge Supervisor)
- **Location:** `edge/db.py`
- **Evidence:** Concurrently writing telemetry check-ins and reading outbox rows produces `sqlite3.OperationalError: database is locked` during high-frequency robot operations.
- **Reproduction:** Run 10 parallel check-in threads against edge SQLite DB.
- **Required Fix:** Enable WAL mode (`PRAGMA journal_mode=WAL;`) and configure `busy_timeout = 5000`.
- **P4 Integration Completed:** Throttled telemetry dispatch in `fault_harness.py`.
- **Remaining Dependency:** P2 database configuration.
- **Acceptance Criterion:** 50 concurrent check-ins execute with zero `database is locked` errors.

---

## 4. P3 Handoffs: Cloud Orchestrator Internals

### HO-P3-01: Task Completion vs Operator Cancellation Race Condition
- **Severity:** High
- **Owning Team:** Person 3 (Cloud Orchestrator)
- **Location:** `cloud/orchestrator.py:finish()` vs `cloud/orchestrator.py:cancel()`
- **Evidence:** If an operator cancels a task (`POST /tasks/{id}/control`, `action: "cancel"`) at the exact millisecond that edge emits `OUTPUT_READY`, race conditions in `cloud/orchestrator.py` can cause inventory stock to be double-incremented (once by cancel return, once by finish restock).
- **Reproduction:** Concurrently invoke `orch.cancel(task)` and `orch.finish(task)` without state lock. Inventory stock increments twice.
- **Required Fix:** Check `task["status"] in TERMINAL_STATUSES` inside `orch.cancel()` and `orch.finish()` before mutating stock.
- **P4 Integration Completed:** `server.py` wraps `_post_control` and `process_escalation` in `_STATE_LOCK` and rejects redundant terminal state updates.
- **Remaining Dependency:** P3 orchestrator state guard.
- **Acceptance Criterion:** Concurrent cancel and finish leave task in exactly one terminal state with correct stock.

### HO-P3-02: Decision-Log Causal Inversion on Edge Replay
- **Severity:** High
- **Owning Team:** Person 3 (Cloud Orchestrator)
- **Location:** `cloud/orchestrator.py:apply_outcome()`
- **Evidence:** Spawned child tasks are occasionally written to the decision log before the parent task's spawning decision row is committed, violating FR-20 causal replay ordering.
- **Reproduction:** Issue composite request that spawns 3 child tasks; inspect timeline rows; child decisions precede parent spawn decision.
- **Required Fix:** Commit parent decision row synchronously before invoking child task allocation loops.
- **P4 Integration Completed:** Monotonic sequence indexing in `server.py:apply_outcome`.
- **Remaining Dependency:** P3 allocation pipeline sequencing.
- **Acceptance Criterion:** Decision log chronological sort matches topological dependency tree.

### HO-P3-03: Bedrock Call Usage Token Accounting Missing in LLM Client
- **Severity:** Medium
- **Owning Team:** Person 3 (Cloud Orchestrator)
- **Location:** `cloud/llm.py:BedrockLLM.call()`
- **Evidence:** The Bedrock response payload contains `usage.inputTokens` and `usage.outputTokens`, but `BedrockLLM` returns only text and hard-codes `tokens_in=0, tokens_out=0` on the returned outcome object.
- **Reproduction:** Execute Bedrock model invocation; inspect `cost.snapshot()`; token counters remain 0.
- **Required Fix:** Extract `resp["usage"]["inputTokens"]` and `resp["usage"]["outputTokens"]` from Bedrock API response and attach to `Outcome`.
- **P4 Integration Completed:** CostBook in `cost.py` implements full pricing math for tokens, Fargate GB-s, and API requests.
- **Remaining Dependency:** P3 Bedrock client return values.
- **Acceptance Criterion:** Live Bedrock calls increment `tokens_in` and `tokens_out` accurately.

### HO-P3-04: Shared State Lock Held During Synchronous LLM Calls
- **Severity:** High
- **Owning Team:** Person 3 (Cloud Orchestrator)
- **Location:** `cloud/orchestrator.py:handle_request()`
- **Evidence:** Holding `_STATE_LOCK` across Bedrock API calls (which take 1.2 to 4.5 seconds) starves edge robot check-in threads, causing false stale-robot timeouts and lease expiries.
- **Reproduction:** Introduce 3s network delay on Bedrock endpoint; edge check-ins queue up and timeout.
- **Required Fix:** Execute model inference outside state lock; acquire lock only during lease commit.
- **P4 Integration Completed:** `server.py:_post_request` invokes `self._decide` outside the global state lock.
- **Remaining Dependency:** P3 internal state locking review.
- **Acceptance Criterion:** Edge check-in latency remains $<50\text{ms}$ while Bedrock inference is active.

### HO-P3-05: SQS Visibility Timeout Lower Than Longest Replanning Slice
- **Severity:** Medium
- **Owning Team:** Person 3 (Cloud Orchestrator)
- **Location:** `cloud/sqs_consumer.py`
- **Evidence:** When a complex multi-turn replanning slice takes $>30\text{s}$, SQS visibility timeout expires, causing the message to be redelivered to a second worker while the first is still processing.
- **Reproduction:** Inject 35s artificial pause into replanning handler; observe duplicate message receipt from SQS.
- **Required Fix:** Heartbeat SQS message via `change_message_visibility` during active multi-turn planning.
- **P4 Integration Completed:** Terraform SQS queue visibility timeout configured to 300 seconds.
- **Remaining Dependency:** P3 heartbeat loop during extended reasoning.
- **Acceptance Criterion:** Single execution of 45-second complex replanning slice without message duplication.

### HO-P3-06: Missing Application EventBridge Notification Publisher
- **Severity:** Low
- **Owning Team:** Person 3 (Cloud Orchestrator)
- **Location:** `cloud/orchestrator.py`
- **Evidence:** Terraform provisions an Amazon EventBridge rule for task status events, but `cloud/orchestrator.py` does not contain a publisher calling `boto3.client("events").put_events`.
- **Reproduction:** Complete task; CloudWatch EventBridge rule invocation count remains 0.
- **Required Fix:** Add optional EventBridge client publishing `NimbusTaskTransition` events on lease state changes.
- **P4 Integration Completed:** EventBridge event bus and IAM permissions declared in `infra/terraform/main.tf`.
- **Remaining Dependency:** P3 application publisher.
- **Acceptance Criterion:** EventBridge rule receives event on task completion in live AWS deployment.
