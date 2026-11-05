# Person 4 (P4) Compliance Matrix

This matrix documents the verification status and implementation evidence for all requirements owned by or shared with Person 4 (P4) per PRD v5.0.

---

## 1. Functional Requirements (FR)

| Requirement | Title & Description | Status | Primary File(s) & Evidence |
| :--- | :--- | :--- | :--- |
| **FR-8** | **Message Deduplication & Version Ordering**<br>Every cloud/edge message carries `message_id` and `lease_version`. Repeated message IDs are dropped; outdated lease versions are rejected with HTTP 409. | **WORKING** | - `server.py` (`_dedupe()`, `process_checkin`, `_post_escalation`)<br>- `contracts.py`<br>- `contracts/topics.md`<br>- `tests/test_api.py` (`test_repeated_message_id_ignored`, `test_outdated_lease_version_is_rejected`) |
| **FR-9** | **Camera Artifact Packaging at Edge**<br>Robots capture and store inspection photos; edge supervisor packages metadata into `OUTPUT_READY` payloads pointing to S3 or local storage. | **WORKING** | - `edge/artifacts/` module<br>- `contracts/robot_subtask.schema.json` (`cmd: photo`)<br>- `contracts/escalation.schema.json` (`result_ref`)<br>- `edge/supervisor/supervisor.py` |
| **FR-10** | **Store-and-Forward Outbox Resilience**<br>During network partitions or link degradation, lease check-ins and escalations buffer in local storage and flush automatically upon reconnection without data loss. | **WORKING** | - `edge/supervisor/outbox_worker.py`<br>- `edge/store/`<br>- `tests/test_outbox_resilience.py` |
| **FR-20** | **Append-Only Attributable Decision Log with Replay**<br>Every decision (requester, cloud, edge, rule) records who, inputs, output, lease_version, and timestamp in append-only JSONL; full audit timeline replayable via API. | **WORKING** | - `store.py` (`append_decision`, `timeline`)<br>- `server.py` (`GET /tasks/{id}/timeline`)<br>- `contracts/decision_log_entry.schema.json`<br>- `tests/test_api.py` (`test_sc1_end_to_end_clarify_and_timeline_order`) |
| **FR-21** | **Plain-Language Requester Web Application**<br>Minimal browser app for hospital staff to enter natural-language requests, answer clarifying questions, track status, and view delivery proofs. | **WORKING** | - `static/index.html`<br>- `static/app.js`<br>- `server.py` (`GET /`, `_send_static`)<br>- `tests/test_api.py` (`test_requester_app_served_at_root`) |
| **FR-22** | **Operator Console & Stop Lease Delivery**<br>Interactive spatial floor map, live fleet positions, network fault badge HUD, and pause/cancel controls that generate and publish a stop lease to robots. | **WORKING** | - `static/operator.html`<br>- `static/operator.js`<br>- `server.py` (`POST /tasks/{id}/control`, `_post_control` calls `orch.stand_down()`)<br>- `tests/test_api.py` (`test_operator_pause_then_cancel_records_intents`)<br>- `tests/test_fault_harness.py` (`test_operator_console_serves_fault_hud`) |

---

## 2. Non-Functional Requirements (NFR)

| Requirement | Title & Description | Status | Primary File(s) & Evidence |
| :--- | :--- | :--- | :--- |
| **NFR-4** | **Cloud Efficiency: C1 Token Budget $\le$ 50% of C2**<br>Leased delegation (C1) limits cloud round trips by authorizing edge autonomy, whereas thin-client relay (C2) invokes the model per sub-task. Cost counters meter calls and tokens. | **WORKING** | - `cost.py` (`llm_calls`, `input_tokens`, `output_tokens`)<br>- `cloud/llm.py` (`CircuitBreakerLLM`, token tracing)<br>- `tests/test_ms2_efficiency.py`<br>- `tests/test_cost_backend.py` |
| **NFR-5** | **Edge Authentication & Least-Privilege IAM**<br>Edge authentication validated $\le$ 50 ms via Bearer tokens; AWS task role scoped strictly to the four DynamoDB tables, SQS queue, outputs S3 prefix, and canonical IoT topics. | **WORKING** | - `server.py` (`_edge_authorised()`, constant-time HMAC check)<br>- `infra/terraform/p3.tf` (least-privilege IAM task role policy)<br>- `tests/test_ms2_security_metrics.py` |
| **NFR-6** | **Byte-Identical Decision Log Replay**<br>Decision logs carry causal sequence and deterministic outputs without ungrounded timestamps; replaying inputs produces byte-identical decision streams. | **WORKING** | - `scripts/replay_check.py`<br>- `store.py`<br>- `cloud/trace.py`<br>- `tests/test_e5_e6.py` (`test_byte_identical_rerun_on_identical_inputs`) |

---

## 3. Operational Flags Reference

| Environment Variable | Default | Purpose |
| :--- | :--- | :--- |
| `NIMBUS_TRANSPORT` | `http` | Transport protocol: `http` (HTTP REST + local JSONL outbox) or `mqtt` (five canonical wire topics). |
| `NIMBUS_DEMO_FAULTS` | `0` | Set to `1` to enable unauthenticated access to `/fault` and `/llm/outage` for local demonstrations and debugging. |
| `NIMBUS_ALLOW_UNKNOWN`| `0` | When `0`, rejects check-ins or heartbeats from unregistered edge cluster IDs with HTTP 404. Set to `1` to permit ad-hoc clusters. |
| `NIMBUS_EDGE_TOKEN` | _(unset)_| When set, requires `Authorization: Bearer <token>` on all edge ingress endpoints (`/escalations`, `/checkins`, `/fleet/*`). |
| `NIMBUS_BACKEND` | `local` | Storage engine: `local` (atomic files in `data/`) or `aws` (DynamoDB + S3 + SQS). |
