# Nimbus Cloud — Input/Output Schema

The cloud is one HTTP service (`server.py`, same binary locally and on AWS Fargate).
Every client — the requester app, the operator console, and each edge supervisor —
speaks **only this HTTP API** (the edge does not use MQTT). On AWS it sits behind an
API Gateway v2 HTTP API with a catch-all `{proxy+}` route, so every path below is
reachable identically whether you hit `http://127.0.0.1:8000` or the deployed
`https://<api-id>.execute-api.<region>.amazonaws.com`.

- Content type: `application/json` on every request/response with a body.
- Companion doc (narrative + scenarios): [EDGE_CLOUD_PROTOCOL.md](EDGE_CLOUD_PROTOCOL.md).
- JSON Schemas for the structured contracts: [`contracts/`](../contracts/)
  (`lease.schema.json`, `escalation.schema.json`, `robot_subtask.schema.json`,
  `decision_log_entry.schema.json`, `topics.md`).

---

## 0. Cross-cutting rules

| Topic | Rule |
| --- | --- |
| **Idempotency** | Every write carries a `message_id`. A replayed id returns `200 {"duplicate": true}` and has no second effect (PRD FR-8). |
| **Version fencing** | Any edge message quoting a lease older than the current one gets `409 {"error":"outdated lease_version","current":N}`. |
| **Auth** | Optional bearer token, off by default. When `NIMBUS_ADMIN_TOKEN`/edge token is set, the edge paths (`/checkins`, `/escalations`, `/fleet/heartbeat`, `/outbox`, `/relay/decide`) require `Authorization: Bearer <token>`. |
| **Errors** | `400` bad/missing fields (body lists the offending paths), `404` unknown task/cluster, `409` stale version / wrong state / wrong mode, `500` invalid lease produced. |
| **Mode** | The service runs in exactly one of `C1`/`C2`/`C3`, fixed at startup by `NIMBUS_MODE`. Some endpoints are mode-gated (noted below). |

---

## 1. Requester / operator app → cloud

### `POST /requests` — create a task
Two mutually exclusive input shapes.

**C1/C2 (plain language):**
```json
{ "text": "Need a pediatric infusion pump at 2B-12, urgent.", "message_id": "req-1" }
```
**C3 (structured form; 409 in any other mode):**
```json
{ "form": { "item": "infusion_pump_paediatric", "destination": "2B",
            "source": null, "priority": "urgent", "deadline_minutes": 12 },
  "message_id": "req-1" }
```
Output `201`:
```json
{ "duplicate": false, "task_id": "task-000001",
  "status": "leased | queued | needs_clarification | rejected",
  "question": null }
```
When `status == "needs_clarification"`, `question` is:
```json
{ "question": "…?", "possible_readings": ["…", "…"], "field": "item" }
```
Errors: `400` (`text`/`form` missing or malformed), `409` (`form` sent to a non-C3 cloud).

### `POST /requests/{task_id}/answer` — answer a clarification (C1/C2)
Input: `{ "text": "Yes, a paediatric pump.", "message_id": "ans-1" }`
Output `201`: `{ "duplicate": false, "task_id", "status", "question": null }`
Errors: `409` if the task is not awaiting an answer.

### `POST /tasks/{task_id}/replan` — operator forces a re-plan
Input: `{ "blocked_zones": ["east_mid_hall"], "reason": "cart", "message_id": "rp-1" }`
Output `201`: `{ "lease": { …lease… } }` (new version), or `replan_failed` detail.

### `POST /tasks/{task_id}/control` — operator pause/cancel (FR-22)
Input: `{ "action": "pause | cancel", "message_id": "ctl-1" }`
Output `201`: `{ "duplicate": false, "accepted": true, "status": "<task status>" }`
Errors: `400` action not pause/cancel, `404` unknown task, `409` already terminal.

### Reads
| Route | Output |
| --- | --- |
| `GET /tasks` | `{ "tasks": [ { task_id, status, spec, lease, … } ] }` |
| `GET /tasks/{id}` | `{ task_id, status, request_text, spec, lease, legs, leg_index, result_ref, summary, question, reason, parent, children, archive_ref, notice }` |
| `GET /tasks/{id}/timeline` | `{ task_id, timeline: [ { who, inputs, output, lease_version, ts } ] }` (decision log for the task) |
| `GET /tasks/{id}/trace` | `{ task_id, events: [ … ] }` (human-readable message trace, incl. model calls) |
| `GET /ui-config` | `{ demo_controls, c3_structured_forms, c3_form_options: { items[], destinations[], sources[], priorities[] } }` |

`spec` = `{ item, destination, destination_anchor, priority, deadline_minutes,
source?, constraints[], rationale, requested_at }`.

---

## 2. Edge → cloud (synchronous HTTP)

### `POST /fleet/heartbeat` — liveness + fleet state (every ~15 s idle)
```json
{ "cluster_id": "east-wing",
  "robots": [ { "id": "robot_1", "state": "idle|busy|failed|charging",
                "zone": "east_mid_hall", "battery": 95.0 } ],
  "stock": { … },           // optional
  "leases": [ … ] }         // optional, sent after an outage for reconciliation
```
Output `201`: `{ "accepted": true, "health": "healthy", "reconcile": { … }? }`.
Error: `404` unknown cluster.

### `POST /checkins` — accept/renew a lease (on accept, then every ~5 s)
```json
{ "cluster_id":"east-wing", "lease_id":"lease-task-000001-v1", "lease_version":1,
  "timestamp":"2026-11-07T10:00:00Z", "message_id":"ci-1", "robots":[ … ] }
```
Output `201`: `{ "duplicate": false, "accepted": true, "renewed_until": "…Z"? }`.
Error: `409` stale version (`{current:N}`).

### `POST /escalations` — the three edge escalations (and operator-visible events)
Common envelope:
```json
{ "type": "OUTPUT_READY | UNEXPECTED_EVENT | CLARIFY",
  "message_id": "…", "lease_version": N, "task_id": "task-000001",
  "payload": { … } }
```
Per-type `payload`:
- **OUTPUT_READY**: `{ "result_ref": "storage://results/task-000001/delivery-proof.jpg" }`
- **UNEXPECTED_EVENT**: `{ "what_happened", "where", "robots_saw": [ … ],
  "segments": [ { "segment_id", "state": "blocked|closed|crowded" } ], "robot_zone"? }`
- **CLARIFY**: `{ "question", "possible_readings": [ … ] }`

Output `201` (shape depends on what the cloud did):
```json
{ "duplicate": false, "accepted": true,
  "replanned": true, "lease_version": 4,     // UNEXPECTED_EVENT that re-planned
  "advanced": true,                          // OUTPUT_READY that advanced a leg
  "answer": "…" }                            // CLARIFY answered from rules/context
```
Errors: `400` (missing `where`/`robots_saw`, `lease_version` 0), `404` unknown task,
`409` stale version.

### `POST /relay/decide` — C2 per-step decision (⚠️ C2 only; `409` otherwise)
The C2 edge has no local model, so before each non-reflex step it asks the cloud.
```json
{ "cluster_id":"east-wing", "lease_id":"lease-task-000001-v1", "lease_version":1,
  "step_index":2, "zone":"east_mid_hall", "target":"east_waiting_approach",
  "blocked_zones":["east_mid_hall"], "attempt":0,
  "situation":"blocked at east_mid_hall", "message_id":"relay-1" }
```
Output `201`:
```json
{ "duplicate": false,
  "action": "proceed | wait | reroute | abort",
  "via_zones": ["…"],            // present for reroute
  "rationale": "…",
  "source": "llm | rules",        // rules = model was down/vetoed, deterministic fallback
  "lease_version": 1 }
```
Errors: `409` (`relay decisions exist only in C2…`), `409` stale version, `404` no live lease, `400` bad `lease_id` format.

### `GET /outbox?active=1` — the edge pulls cloud→edge messages (every ~2 s)
Output: `{ "outbox": [ { "topic": "nimbus/<cluster>/<kind>", "payload": { … } } ] }`.
`active=1` omits leases for already-finished tasks (stop leases always kept).

---

## 3. Cloud → edge messages (the `payload`s inside `/outbox`)

| Topic | Payload |
| --- | --- |
| `nimbus/<cluster>/lease` | Full lease (schema `contracts/lease.schema.json`): `lease_id, version, priority, deadline_ts, expiry_ts, allowed_zones[], forbidden_zones[], speed_limit, assigned_robots[], task_sequence[{cmd: go_to\|wait\|stop\|photo\|beep\|report, robot, params{to, via[], allowed_zones[]…}}], cluster_id, when_to_escalate[]` |
| `nimbus/<cluster>/renewal` | `{ lease_id, version, expiry_ts, message_id }` |
| `nimbus/<cluster>/probe` | `{ type:"PROBE", cluster_id, message_id, ts }` |
| `nimbus/<cluster>/notice` | `{ type, message_id, ts, … }` — `MAP`→`{text,cluster_id}`; `BUNDLE`→`{task_id,lease_id,lease_version,bundle}`; `ANSWER`→`{text,task_id,lease_version}` (reply to a CLARIFY) |

---

## 4. Observability / config / fault-injection

| Route | I/O |
| --- | --- |
| `GET /fleet` | `{ fleet[], places[], mode:"C1\|C2\|C3", llm:"<model id>", pack:"<id>" }` — quickest way to confirm a deployment's mode + LLM backend |
| `GET /metrics` | counters + `gauges{queue_depth, live_leases, clusters{…health}}` + latency p50/p95 (NFR-1) |
| `GET /cost` | per-task cost-formula counters (tokens, Fargate, IoT, DDB, SQS, S3, transfer, API GW, CW) |
| `GET /pack` | active pack: items, priorities, zones, steps/tools |
| `GET /map.json` | nodes, edges, positions, fleet overlay for the map view |
| `GET /notifications` | operator/requester notifications |
| `GET /inventory` | stock snapshot |
| `GET /llm-status` | `{ model, bedrock_configured, circuit_breaker, fault_down }` |
| `GET /trace` | recent cross-component message trace (`?after=<seq>`) |
| `GET /schemas/<name>` | the published JSON Schemas |
| `POST /llm/outage` | `{ "seconds": N }` → `201 {model_down, seconds}` (E5: force the model unavailable; rules take over, FR-18) |
| `POST /map/reopen` | `{ "node": "…" }` → `201 {reopened}` |
| `POST /fleet/stock` · `POST /inventory/stock` · `POST /inventory/reset` · `POST /reset-memory` | admin/demo state resets |

---

## 5. What differs by configuration

The **wire schema is identical** across C1/C2/C3; only *who decides* changes, and two
endpoints are mode-gated:

| | C1 | C2 | C3 |
| --- | --- | --- | --- |
| `POST /requests` ingress | `text` (LLM) | `text` (LLM) | `form` (no model) |
| Initial allocation | LLM proposes → rule checker | LLM proposes → rule checker | classical allocator → rule checker |
| Per-step blockage decision | **edge SLM** (no cloud call) | **`POST /relay/decide`** → cloud LLM per step | edge finite-state rules |
| `/relay/decide` | `409` | **active** | `409` |
| `/requests {form}` | `409` | `409` | **active** |

So a cloud deployment serves C2 correctly **iff** it was started with
`NIMBUS_MODE=C2` (that flips `/relay/decide` on and uses the LLM in the
`decide_step` path). No code change is required — see §C2 deployment in the chat
summary / `README`.
