# Nimbus Tier Architecture and P4 Boundary

This document outlines the three-tier architectural model from Nimbus PRD v5.0 and details how the Person 4 (P4) codebase implements and guards the cloud boundary.

## Three-Tier System Architecture

Nimbus decomposes hospital mobile-robot operations into three distinct tiers: Cloud, Edge, and Robot.

```
+-------------------------------------------------------------------+
|                            CLOUD TIER                             |
|  - Ingress API Gateway (HTTP)                                     |
|  - Bedrock LLM Orchestrator (Person 3) / Stand-in (standin.py)    |
|  - Deterministic Rule Checker & Task Scheduler                    |
|  - Persistence: DynamoDB (store.py stands in with JSON/JSONL)    |
|  - Event Broker: EventBridge & SQS                                |
|  - Audit & Traceability: Append-only Decision Log (FR-20)          |
+-------------------------------------------------------------------+
                                  ^  |
                Escalations &     |  | Time-limited Authority Leases
                Check-ins (IoT)   |  | (contracts/lease.schema.json)
                                  |  v
+-------------------------------------------------------------------+
|                            EDGE TIER                              |
|  - Per-wing Edge Cluster (Supervisor + Edge SLM via llama.cpp)   |
|  - Local Task Sequencing & Lease Execution                        |
|  - Autonomy during network disconnection                          |
|  - Escalations: CLARIFY, UNEXPECTED_EVENT, OUTPUT_READY           |
|  - Regular Lease Renewal Check-ins (no LLM in renewal path)      |
+-------------------------------------------------------------------+
                                     |
                                     | Primitive Robot Sub-tasks
                                     | (go_to, wait, stop, photo, etc.)
                                     v
+-------------------------------------------------------------------+
|                            ROBOT TIER                             |
|  - Mobile Robots (PiCar-X hardware or Gazebo simulation)          |
|  - Motion execution strictly constrained by allowed zones & speed |
|  - Dead-man parking safety when lease expires                     |
+-------------------------------------------------------------------+
```

### 1. Cloud Tier
The cloud tier provides global coordination, high-capacity intelligence, and durable records:
- Translates plain-language ward requests into structured hospital tasks.
- Resolves ambiguities by posing clarifying questions to staff (FR-13).
- Generates time-limited authority leases containing allowed zones, speed limits, robot assignments, and escalation criteria.
- Validates every proposed assignment through a deterministic rule checker before lease publication.
- Maintains an append-only decision log (FR-20) recording who decided (cloud, edge, rule, or requester) and under which lease version.
- Re-plans tasks when edge clusters send UNEXPECTED_EVENT or CLARIFY escalations.
- Infrastructure runs as an always-on ECS Fargate container (rejecting Lambda per PRD Fig 4).

### 2. Edge Tier
Each hospital wing hosts an edge cluster (e.g. east-wing):
- Consists of a cluster supervisor and a local edge SLM running on local compute.
- Receives authority leases from the cloud and executes them autonomously, even when wide-area connectivity drops.
- Translates lease task sequences into individual robot sub-tasks.
- Emits three types of structured escalations back to the cloud:
  - `CLARIFY`: When local instructions or physical surroundings are too ambiguous for the edge SLM.
  - `UNEXPECTED_EVENT`: When physical obstacles, closed fire doors, robot failures, or deadline risks block progress.
  - `OUTPUT_READY`: When a delivery is completed or task artifacts (such as photos) are ready.
- Sends regular check-in messages to renew active leases without involving cloud LLMs.

### 3. Robot Tier
Physical robots (or Gazebo simulation nodes):
- Execute primitive sub-tasks: `go_to`, `wait`, `stop`, `photo`, `beep`, `report`.
- Do not hold end-to-end task logic or communicate directly with the cloud.
- Bound strictly by the enclosing lease: never exceed speed limits, never cross forbidden zones, and safely park if a lease expires before renewal.

## Person 4 Implementation Boundary

The P4 slice establishes the software and contract boundary that isolates the cloud tier while decoupling it from edge and robot implementations.

### 1. HTTP Ingress and API Routes (`server.py`)
`server.py` implements the cloud-facing API using Python's standard library `ThreadingHTTPServer`. It provides:
- Request Ingress: `POST /requests` and `POST /requests/{id}/answer` for staff interaction, enforcing FR-8 message deduplication.
- Edge Escalation Ingress: `POST /escalations` validating against `contracts/escalation.schema.json`, dropping stale lease versions (409), and completing tasks on `OUTPUT_READY`.
- Edge Check-in Ingress: `POST /checkins` logging `edge` decision rows and applying FR-8 deduplication.
- Control Ingress: `POST /tasks/{id}/control` logging operator pause/cancel intents without direct robot delivery.
- Inspection & Timeline: `GET /tasks/{id}`, `GET /tasks`, and `GET /tasks/{id}/timeline` for replaying audit logs.
- Contract Distribution: `GET /schemas/{name}.json` exposing canonical wire schemas.
- Outbox Feed: `GET /outbox` serving leases to edge consumers in lieu of live MQTT brokers.
- Cost Monitoring: `GET /cost` exposing PRD Sec. 7 formula counters.
- Static Web UIs: Serving the requester application (FR-21) and operator console (FR-22 slice).

### 2. Wire Contracts (`contracts/` and `contracts.py`)
The canonical interface across tiers is defined with JSON Schema Draft 2020-12:
- `contracts/lease.schema.json`: Formal cloud-to-edge authority contract specifying lease IDs, monotonic version numbers, priority levels, deadline and expiry timestamps, allowed and forbidden zones, speed limits, robot assignments, and escalation conditions.
- `contracts/escalation.schema.json`: Formal edge-to-cloud message specifying `CLARIFY`, `UNEXPECTED_EVENT`, and `OUTPUT_READY` payloads.
- `contracts/robot_subtask.schema.json`: Robot action primitives accepted by individual robots.
- `contracts/decision_log_entry.schema.json`: Audit log schema capturing actors (`requester`, `cloud`, `edge`, `rule`), inputs, outputs, lease versions, and timestamps.
- `contracts/topics.md`: Topic naming format for MQTT / IoT Core (`nimbus/{cluster_id}/lease`, `nimbus/{cluster_id}/escalation`, `nimbus/{cluster_id}/checkin`).
- `contracts.py`: Validates payloads against schemas and enforces semantic cross-field invariants (`deadline_ts <= expiry_ts`).

### 3. Persistence Abstraction (`store.py`)
`store.py` encapsulates state storage with thread-safe file operations:
- `data/tasks.json`: Atomic full-state storage for task records.
- `data/decisions.jsonl`: Append-only stream of decision rows for deterministic audit replay.
- `data/outbox.jsonl`: Append-only queue of issued leases.
This file provides a clean boundary that Person 3 can swap directly for DynamoDB table calls.

### 4. Cloud Decision Stand-in (`standin.py`)
`standin.py` acts as a deterministic placeholder for Person 3's Bedrock orchestrator:
- Encapsulates SC-1 scenario rules: recognizes pediatric bed requests (e.g., bed 2B-12) and generates a clarifying question if the pump type is omitted.
- Generates compliant lease v1 documents upon answer resolution.
- Separates heuristic rules from HTTP handling, ensuring Person 3 only needs to replace this file.

### 5. Infrastructure Skeleton (`infra/terraform/`)
`infra/terraform/` defines the production AWS topology matching PRD Fig 4:
- VPC with two private subnets.
- ECS Fargate cluster and service for running the orchestrator.
- HTTP API Gateway with route definitions (mock integration point ready for VPC link proxy).
- DynamoDB tables for leases, tasks, robot status, and decision logs.
- SQS escalation queues and EventBridge routing bus.
- S3 bucket for task output artifacts.
- Least-privilege IAM roles granting scoped access to specific ARNs.
- Provider lockfile checked in; configuration validated via `terraform validate`.
