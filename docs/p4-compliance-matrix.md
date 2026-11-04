# Person 4 (P4) Compliance Matrix

This matrix documents the verification status, current code paths, test suites, and implementation evidence for all requirements owned by or shared with Person 4 (P4) per PRD v5.0.

---

## 1. Functional Requirements (FR)

| Requirement | Title & Description | Status | Current Code Path | Automated Tests & Verification |
| :--- | :--- | :--- | :--- | :--- |
| **FR-8** | **Message Deduplication & Lease Version Ordering**<br>Every cloud/edge message carries `message_id` and `lease_version`. Duplicate message IDs are dropped idempotently; outdated lease versions are rejected with HTTP 409 Conflict. | **WORKING** | - [`server.py`](../server.py) (`_dedupe()`, `process_checkin`, `_post_escalation`)<br>- [`contracts.py`](../contracts.py) (`validate_message`)<br>- [`contracts/topics.md`](../contracts/topics.md) (canonical topics specification) | - `tests/test_api.py::test_repeated_message_id_ignored`<br>- `tests/test_api.py::test_outdated_lease_version_is_rejected`<br>- `tests/test_api.py::test_checkin_with_valid_and_invalid_lease_version` |
| **FR-9** | **Camera Artifact Packaging at Edge**<br>Robots capture and store inspection photos; edge supervisor packages metadata into `OUTPUT_READY` payloads pointing to S3 or local storage. | **WORKING** | - [`edge/artifacts/`](../edge/artifacts/) (artifact storage module)<br>- [`contracts/robot_subtask.schema.json`](../contracts/robot_subtask.schema.json) (`cmd: photo`)<br>- [`contracts/escalation.schema.json`](../contracts/escalation.schema.json) (`result_ref`)<br>- [`edge/supervisor/supervisor.py`](../edge/supervisor/supervisor.py) | - `tests/test_api.py::test_sc1_end_to_end_clarify_and_timeline_order`<br>- `edge/tests/test_supervisor.py` |
| **FR-10** | **Store-and-Forward Outbox Resilience**<br>During network partitions or link degradation, lease check-ins and escalations buffer in local storage and flush automatically upon reconnection without message loss. | **WORKING** | - [`edge/supervisor/outbox_worker.py`](../edge/supervisor/outbox_worker.py)<br>- [`scripts/fault_harness.py`](../scripts/fault_harness.py) (`FaultProxy`)<br>- [`docs/e4-e5-evidence.md`](e4-e5-evidence.md) (E4/E5 harness instructions)<br>- [`docs/faults.md`](faults.md) | - `tests/test_fault_harness.py::test_offline_quality_edge_sim_hold_and_replay_on_reconnect`<br>- `tests/test_fault_harness.py::test_offline_quality_edge_supervisor_hold_and_replay_on_reconnect`<br>- `tests/test_fault_harness.py::test_preset_profiles_exist` |
| **FR-20** | **Append-Only Attributable Decision Log with Replay**<br>Every decision (requester, cloud, edge, rule) records who, inputs, output, lease_version, and timestamp in append-only JSONL; full audit timeline replayable via API. | **WORKING** | - [`store.py`](../store.py) (`append_decision`, `timeline`)<br>- [`server.py`](../server.py) (`GET /tasks/{id}/timeline`)<br>- [`contracts/decision_log_entry.schema.json`](../contracts/decision_log_entry.schema.json)<br>- [`scripts/replay_check.py`](../scripts/replay_check.py) | - `tests/test_api.py::test_sc1_end_to_end_clarify_and_timeline_order`<br>- `tests/test_api.py::test_decision_log_append_and_schema`<br>- `tests/test_e5_e6.py::test_byte_identical_rerun_on_identical_inputs` |
| **FR-21** | **Plain-Language Requester Web Application**<br>Minimal browser app for hospital staff to enter natural-language requests, answer clarifying questions, track status, and view delivery proofs. | **WORKING** | - [`static/index.html`](../static/index.html)<br>- [`static/app.js`](../static/app.js)<br>- [`server.py`](../server.py) (`GET /`, `_send_static`) | - `tests/test_api.py::test_requester_app_served_at_root`<br>- `tests/test_api.py::test_requester_app_assets` |
| **FR-22** | **Operator Console & Stop Lease Delivery**<br>Interactive spatial floor map, live fleet positions, network fault badge HUD, and pause/cancel controls that generate and publish a stop lease to robots. | **WORKING** | - [`static/operator.html`](../static/operator.html)<br>- [`static/operator.js`](../static/operator.js)<br>- [`server.py`](../server.py) (`POST /tasks/{id}/control`, `_post_control` calls `orch.stand_down()`) | - `tests/test_api.py::test_operator_pause_then_cancel_records_intents`<br>- `tests/test_fault_harness.py::test_operator_console_serves_fault_hud`<br>- `tests/test_fault_harness.py::test_operator_console_displays_active_fault_telemetry` |

---

## 2. Non-Functional Requirements (NFR)

| Requirement | Title & Description | Status | Current Code Path | Automated Tests & Verification |
| :--- | :--- | :--- | :--- | :--- |
| **NFR-4** | **Cloud Efficiency & 10-Term PRD Cost Formula**<br>Leased delegation (C1) limits cloud round trips by authorizing edge autonomy. Complete PRD Sec. 7 formula tracks all 10 terms ($T_{\text{in}}$, $T_{\text{out}}$, $F$, $M$, $W$, $Q$, $S$, $D$, $G$, $K$) divided by $N_{\text{tasks}}$. | **WORKING** | - [`cost.py`](../cost.py) (`COUNTERS`, `calculate_cost_per_task`, `CostBook`)<br>- [`cloud/llm.py`](../cloud/llm.py) (`CircuitBreakerLLM`, token tracing) | - `tests/test_cost_backend.py::test_calculate_cost_per_task_all_ten_terms`<br>- `tests/test_cost_backend.py::test_calculate_cost_per_task_zero_tasks_safe`<br>- `tests/test_cost_backend.py::test_calculate_cost_per_task_pricing_aliases`<br>- `tests/test_cost_backend.py::test_calculate_cost_per_task_with_cost_book_integration`<br>- `tests/test_ms2_efficiency.py` |
| **NFR-5** | **Edge Authentication & Least-Privilege IAM**<br>Edge authentication validated $\le$ 50 ms via Bearer tokens; AWS task role scoped strictly to the state tables, SQS queue, outputs S3 prefix, Secrets Manager, and canonical IoT topics. | **WORKING** | - [`server.py`](../server.py) (`_edge_authorised()`, constant-time HMAC check)<br>- [`infra/terraform/main.tf`](../infra/terraform/main.tf) & [`p3.tf`](../infra/terraform/p3.tf) (IAM task role policies) | - `tests/test_api.py::test_edge_auth_bearer_token`<br>- `tests/test_ms2_security_metrics.py`<br>- `infra/terraform/` (`terraform validate`) |
| **NFR-6** | **Byte-Identical Decision Log Replay**<br>Decision logs carry causal sequence and deterministic outputs without ungrounded timestamps; replaying inputs produces byte-identical decision streams. | **WORKING** | - [`scripts/replay_check.py`](../scripts/replay_check.py)<br>- [`store.py`](../store.py)<br>- [`cloud/trace.py`](../cloud/trace.py) | - `tests/test_e5_e6.py::test_byte_identical_rerun_on_identical_inputs`<br>- `tests/test_api.py::test_decision_log_append_and_schema` |

---

## 3. Operational Flags Reference

| Environment Variable | Default | Purpose |
| :--- | :--- | :--- |
| `NIMBUS_TRANSPORT` | `http` | Transport protocol: `http` (HTTP REST + local JSONL outbox) or `mqtt` (five canonical wire topics via local broker). |
| `NIMBUS_DEMO_FAULTS` | `0` | Set to `1` to enable unauthenticated access to `/fault` and `/llm/outage` for local demonstrations and live HUD telemetry. |
| `NIMBUS_ALLOW_UNKNOWN`| `0` | When `0`, rejects check-ins or heartbeats from unregistered edge cluster IDs with HTTP 404. Set to `1` to permit ad-hoc clusters. |
| `NIMBUS_EDGE_TOKEN` | _(unset)_| When set, requires `Authorization: Bearer <token>` on all edge ingress endpoints (`/escalations`, `/checkins`, `/fleet/*`). |
| `NIMBUS_BACKEND` | `local` | Storage engine: `local` (atomic files in `data/`) or `aws` (DynamoDB + S3 + SQS). |
| `NIMBUS_COST_BACKEND` | `local` | Cost persistence engine: `local` (`data/cost.json`) or `dynamodb` (state table atomic `ADD` counter). |
