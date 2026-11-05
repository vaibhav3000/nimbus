# Cloud-Edge Transport Modes

Nimbus supports two switchable cloud-edge transport modes: the default local HTTP + JSONL outbox mode, and an optional local MQTT transport mode for Milestone 2 testing.

---

## 1. Transport Modes Overview

| Feature | Default Mode (`NIMBUS_TRANSPORT=http` or unset) | MQTT Mode (`NIMBUS_TRANSPORT=mqtt`) |
| :--- | :--- | :--- |
| **Activation** | Default out-of-the-box behavior | Set environment variable `NIMBUS_TRANSPORT=mqtt` |
| **Cloud $\rightarrow$ Edge Egress** | Appends to `data/outbox.jsonl` (polled via `GET /outbox`) | Publishes to MQTT broker **and** mirrors to `data/outbox.jsonl` |
| **Edge $\rightarrow$ Cloud Ingress** | HTTP REST (`POST /escalations`, `POST /checkins`) | Subscribes to MQTT topics (`escalation`, `checkin`) + HTTP REST |
| **Dependencies** | Python standard library only | Local broker (Mosquitto via `paho-mqtt`) or built-in stand-in |

---

## 2. Canonical Topics (`contracts/topics.md`)

All MQTT messaging conforms exactly to the five canonical topics defined in `contracts/topics.md`, scoped by edge cluster `{cluster_id}` (e.g. `east-wing`):

1. **`nimbus/{cluster_id}/lease` (Cloud $\rightarrow$ Edge)**
   Carries the lease document (`contracts/lease.schema.json`). Published once per task issue.
2. **`nimbus/{cluster_id}/renewal` (Cloud $\rightarrow$ Edge)**
   Sent when a check-in extends a live lease. Updates `expiry_ts` while keeping the lease version unchanged.
3. **`nimbus/{cluster_id}/probe` (Cloud $\rightarrow$ Edge)**
   Health check ping sent to quiet or stale clusters before task reallocation.
4. **`nimbus/{cluster_id}/escalation` (Edge $\rightarrow$ Cloud)**
   Carries escalation documents (`contracts/escalation.schema.json`) for `OUTPUT_READY`, `UNEXPECTED_EVENT`, or `CLARIFY`.
5. **`nimbus/{cluster_id}/checkin` (Edge $\rightarrow$ Cloud)**
   Regular renewal heartbeat containing current lease status and optional robot/stock telemetry.

---

## 3. Ingress & Egress Parity

- **Outgoing Publications (Mirrored):** When `NIMBUS_TRANSPORT=mqtt` is active, leases, renewals, and probes are published to the MQTT broker and simultaneously appended to `data/outbox.jsonl`. This preserves `GET /outbox` access for local inspection, test fixtures, and web UIs.
- **Incoming Messages:** Messages received on `nimbus/+/escalation` and `nimbus/+/checkin` run through the exact same processing logic as `POST /escalations` and `POST /checkins`:
  - Enforces FR-8 `message_id` deduplication.
  - Rejects outdated `lease_version` (stale lease check).
  - Validates documents against wire schemas (`contracts/*.schema.json`).
  - Records FR-20 append-only decision log rows with identical metadata.
  - Records Section 7 cost counters.

---

## 4. Starting a Local Broker

### Option A: Eclipse Mosquitto via Docker (Recommended)
```bash
docker run -it --rm -p 1883:1883 eclipse-mosquitto
```

### Option B: Native Mosquitto
```bash
mosquitto -v
```

### Option C: Zero-Dependency In-Process Stand-in
If `paho-mqtt` is not installed or `NIMBUS_MQTT_STANDIN=1` is specified, Nimbus automatically falls back to an in-process stand-in broker (`InMemoryBroker`). This allows unit and smoke tests to execute with zero external daemons.

---

## 5. Configuration Variables

- `NIMBUS_TRANSPORT`: Set to `mqtt` to enable MQTT transport. Defaults to `http`.
- `NIMBUS_MQTT_HOST`: Broker hostname. Defaults to `localhost`.
- `NIMBUS_MQTT_PORT`: Broker port. Defaults to `1883`.
- `NIMBUS_MQTT_STANDIN`: Set to `1` or `true` to force using the in-process stand-in broker regardless of `paho-mqtt` presence.
- `NIMBUS_IOT_ENDPOINT`: Retained for AWS IoT Core integration in `cloud/aws_store.py` (when `NIMBUS_BACKEND=aws`).

---


---

## 6. AWS IoT Core Status (IAM-Ready by Design)

- **Default Mode:** Default transport is HTTP REST + local JSONL outbox (`data/outbox.jsonl`), requiring zero external daemons.
- **MQTT Transport:** Activated with `NIMBUS_TRANSPORT=mqtt`. Works out-of-the-box with a local Mosquitto broker (`localhost:1883`) or the zero-dependency in-process `InMemoryBroker`.
- **AWS IoT Core Provisioning:** The AWS deployment is **IAM-ready** but deliberately **not provisioned with static IoT Things or rules**:
  - `infra/terraform/p3.tf` grants the ECS task role least-privilege IAM permissions for `iot:Connect`, `iot:Publish`, `iot:Subscribe`, and `iot:Receive` scoped strictly to the five canonical topics (`arn:aws:iot:${region}:${account}:topic/nimbus/*/${topic}`).
  - Static things, certificates, and rules are not provisioned in Terraform by design: edge clusters and simulated robots authenticate dynamically, and local Mosquitto/HTTP enables 100% deterministic, hermetic grading without incurring live AWS IoT Core connectivity charges.

## 7. Integration Guide for Edge (P2) and Cloud (P3)

For detailed wiring diagrams, topic subscription patterns, and cost counter hooks, see [docs/integration.md](integration.md).

