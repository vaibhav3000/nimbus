# Nimbus Integration Points (P2 Edge & P3 Cloud)

This document provides contract-stable integration instructions and conventions for **Person 2 (Edge Tier & Local Autonomy)** and **Person 3 (Cloud Brain & Orchestrator)** to interface with the Nimbus core server, switchable MQTT transport, and pluggable cost accounting backend.

---

## 1. Person 2 (Edge Tier) Integration

Currently, the Edge Tier communicates with the Nimbus Cloud server using the **Default HTTP Mode** (`/outbox`, `/escalations`, `/checkins`). The **MQTT Transport Mode** specifications below document the canonical topic contracts (`contracts/topics.md`) for when Person 2 adds an MQTT client to the supervisor. The contract shapes (`contracts/*.schema.json`) and message semantics remain identical across both modes.

### A. Default Mode: HTTP REST + Outbox Polling
The existing edge supervisor communicates over the standard Python stdlib HTTP endpoints (Milestone 1 default).

| Direction | Purpose | Endpoint | Method | Payload / Response |
| :--- | :--- | :--- | :--- | :--- |
| **Cloud $\rightarrow$ Edge** | Poll pending leases & probes | `/outbox` | `GET` | `{"outbox": [{"topic": str, "payload": dict}]}` |
| **Edge $\rightarrow$ Cloud** | Report mission complete / block | `/escalations` | `POST` | `contracts/escalation.schema.json` |
| **Edge $\rightarrow$ Cloud** | Renewal heartbeat & telemetry | `/checkins` | `POST` | `contracts/checkin.schema.json` |
| **Edge $\rightarrow$ Cloud** | Fleet position & battery pulse | `/fleet/heartbeat` | `POST` | `{"cluster_id": str, "robots": [...]}` |

#### Outbox Polling Workflow
1. The Edge Supervisor periodically polls `GET /outbox` (or inspects rows matching its `cluster_id`).
2. Each outbox item contains a `topic` and a `payload`.
   - `nimbus/{cluster_id}/lease`: Ingest authority lease, validate against `contracts/lease.schema.json`, and execute mission sequence.
   - `nimbus/{cluster_id}/renewal`: Update existing lease expiry timestamp (`expiry_ts`).
   - `nimbus/{cluster_id}/probe`: Respond immediately with a `/fleet/heartbeat` to signal edge readiness.

---

### B. MQTT Transport Mode
When `NIMBUS_TRANSPORT=mqtt` is set on the server, incoming and outgoing traffic is also bridged directly through the local MQTT broker (e.g. Mosquitto on port 1883).

#### Topic Subscriptions (Cloud $\rightarrow$ Edge)
Edge supervisors should subscribe to the following topics scoped to their `{cluster_id}` (e.g., `east-wing`):

1. **`nimbus/{cluster_id}/lease`**
   - **Trigger:** Cloud issues a new authority lease for a task.
   - **Payload:** Raw JSON lease document conforming to `contracts/lease.schema.json`.
   - **Edge Action:** Validate schema, check monotonic version > current version, persist in local SQLite store, and dispatch sub-tasks.

2. **`nimbus/{cluster_id}/renewal`**
   - **Trigger:** Cloud grants an extension on an active lease in response to an edge check-in.
   - **Payload:** Updated lease document with extended `expiry_ts` (version unchanged).
   - **Edge Action:** Extend local watchdog timer to prevent safe parking.

3. **`nimbus/{cluster_id}/probe`**
   - **Trigger:** Cloud health check ping before reallocating tasks.
   - **Payload:** `{"probe_id": str, "timestamp": str}`.
   - **Edge Action:** Emit a check-in or fleet heartbeat message.

#### Topic Publications (Edge $\rightarrow$ Cloud)
Edge supervisors publish structured documents directly to:

1. **`nimbus/{cluster_id}/escalation`**
   - **Payload:** Document matching `contracts/escalation.schema.json`.
   - **Types:**
     - `OUTPUT_READY`: Mission complete. Includes `payload.result_ref` (proof image or sensor digest).
     - `UNEXPECTED_EVENT`: Dynamic obstacle or zone blocked. Includes `payload.what_happened` and `payload.segments` to trigger cloud re-planning.
     - `CLARIFY`: Ambiguous environment or missing authorization.

2. **`nimbus/{cluster_id}/checkin`**
   - **Payload:** Document matching `contracts/checkin.schema.json`.
   - **Required Fields:** `cluster_id`, `lease_id`, `lease_version`, `timestamp`, `message_id`.
   - **Optional Fields:** `robots` array containing live coordinate `pos: [x, y, z]`, battery level, and operational state.

#### Parity Guarantee
- When MQTT is active, the Cloud server automatically mirrors outgoing leases to `data/outbox.jsonl` so local inspection and HTTP fallbacks never break.
- Incoming MQTT messages pass through the exact same deduplication (`message_id`), schema validation, decision-logging, and cost-accounting pipelines as HTTP `POST` requests.

---

## 2. Person 3 (Cloud Tier) Integration

### A. Pluggable Cost Accounting Seam (`cost.py`)
All PRD Section 7 cost equation counters are managed centrally behind `CostBook` and `make_cost_backend()` in `cost.py`.

```
Cost = [T_in*p_in + T_out*p_out + F*p_fargate + M*p_iot + W*p_ddb + Q*p_sqs
        + S*p_s3 + D*p_transfer + G*p_apigw + K*p_cw] / N_tasks
```

#### Public Surface (No Caller Changes Needed)
Callers in Person 3 cloud code or `server.py` continue to use `CostBook` without change:

```python
from cost import CostBook

# In server.py or orchestrator initialization:
cost = CostBook(data_dir)

# Record counters anywhere in the cloud pipeline:
cost.record(
    input_tokens=150,
    output_tokens=32,
    iot_messages=1,
    dynamodb_ops=2,
    sqs_messages=1,
)

# Fetch complete snapshot for GET /cost:
counters = cost.snapshot()
```

#### Activating DynamoDB Cost Backend
By default, counters persist to `data/cost.json` via `LocalFileCostBackend`. To switch to DynamoDB:

1. Set `NIMBUS_COST_BACKEND=dynamodb` (or alias `NIMBUS_COST_BACKEND=aws`). Note that `NIMBUS_BACKEND=aws` alone does **not** activate the DynamoDB cost backend to prevent accidental cloud calls during local development.
2. The server instantiates `DynamoCostBackend.from_env()`.
3. In DynamoDB, counters are updated atomically using `UpdateExpression="ADD #c :v"` on item `Key={"pk": {"S": "COST#counters"}}` in the DynamoDB state table (`{prefix}-state`).
4. If DynamoDB is temporarily unreachable or offline during local testing, `DynamoCostBackend` automatically logs a warning and tracks counts in memory with zero crashes.

#### Feeding Real Bedrock Token Usage
In Milestone 2, Person 3 replaces stand-in heuristic tokens with real Bedrock Converse metrics. Pass usage directly from `LLMResult` into `cost.record`:

```python
# cloud/orchestrator.py or server.py
outcome = orch.handle_request(...)
cost.record(
    input_tokens=outcome.tokens_in,
    output_tokens=outcome.tokens_out,
    iot_messages=len(outcome.publish),
    dynamodb_ops=len(outcome.rows) + 2,
    fargate_gbs=0.001,
    cloudwatch_metrics=1,
)
```

---

## 3. Environment Variables Quick Reference

| Variable | Values | Default | Purpose |
| :--- | :--- | :--- | :--- |
| `NIMBUS_TRANSPORT` | `http`, `mqtt` | `http` | Selects cloud-edge transport layer |
| `NIMBUS_MQTT_HOST` | hostname / IP | `localhost` | MQTT broker address |
| `NIMBUS_MQTT_PORT` | port number | `1883` | MQTT broker port |
| `NIMBUS_MQTT_STANDIN` | `1`, `0` | `0` | Force in-process stand-in broker (no Mosquitto needed) |
| `NIMBUS_COST_BACKEND` | `local`, `dynamodb`, `aws` | `local` | Explicit cost storage engine selection (`dynamodb` or alias `aws`) |
| `NIMBUS_BACKEND` | `local`, `aws` | `local` | General backend selection (routes tasks/state store to AWS; does not alter cost backend) |
| `NIMBUS_DDB_PREFIX` | string | `nimbus-ds252` | Table prefix for DynamoDB state and tasks |
