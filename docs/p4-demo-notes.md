# Nimbus P4 Demo & Grader Notes

This document clarifies key Person 4 architectural choices and demo scope boundaries for reviewers and external graders.

---

## 1. Cloud-Edge Transport & IoT Core Design

- **Default Transport**: HTTP REST + local JSONL outbox (`data/outbox.jsonl`, `data/tasks.json`).
  - No external services or broker daemons required.
  - Zero-cost, hermetic, and completely deterministic for local evaluation.
- **MQTT Transport**:
  - Activated by setting `NIMBUS_TRANSPORT=mqtt`.
  - Works with a local Eclipse Mosquitto broker (`localhost:1883`) or with the built-in zero-dependency `InMemoryBroker`.
  - Conforms to the five canonical wire topics defined in `contracts/topics.md` (`lease`, `renewal`, `probe`, `escalation`, `checkin`).
- **AWS IoT Core Status (IAM-Ready by Design)**:
  - In `infra/terraform/p3.tf`, the ECS Fargate task role is explicitly granted least-privilege permissions for `iot:Connect`, `iot:Publish`, `iot:Subscribe`, and `iot:Receive` restricted to `arn:aws:iot:${var.aws_region}:${account_id}:topic/nimbus/*/${topic}`.
  - Static IoT Things, certificates, and SQL topic rules are **not provisioned in Terraform by design** for the demo:
    1. Edge clusters and mobile robots negotiate credentials dynamically.
    2. Hermetic evaluation and automated tests run against local Mosquitto or in-process brokers to avoid incurring persistent cloud IoT connection fees ($0.08/million minutes).

---

## 2. Operator Console & Stop Lease Delivery (FR-22)

- In the operator console (`GET /operator`), pressing **Pause** or **Cancel** triggers `POST /tasks/{id}/control`.
- The handler:
  1. Records the operator intent in the append-only decision log (`who: rule`, `decision: operator_pause` or `operator_cancel`).
  2. If the task has a live lease, calls `orchestrator.stand_down()`, which issues and publishes a **stop lease** (`[{"cmd": "stop"}]`, version incremented) to the edge cluster via the active transport (`nimbus/{cluster_id}/lease` and `data/outbox.jsonl`).
  3. Canceling additionally releases reserved robots and zones back to the fleet registry.

---

## 3. Network Fault Harness & Campus Traces (E4–E6)

- The fault harness (`scripts/fault_harness.py`) intercepts HTTP (port 8001 → 8000) or MQTT (port 1884 → 1883) traffic to inject latency, jitter, loss, and disconnection.
- Preset profiles include `healthy`, `mild_wan`, `shielded_room`, and `basement_outage`.
- **Note on IISc Traces**: The traces in `traces/` (`iisc_corridor.csv`, `iisc_lift.csv`, `iisc_basement.csv`) are short synthetic examples for local testing, not full 10 Hz physical recordings. They validate the dynamic time-series replay logic in `TraceReplayer` without requiring gigabytes of raw RF telemetry.

---

## 4. Terraform Infrastructure & CloudWatch Skeleton

- The Terraform configuration in `infra/terraform/` defines:
  - VPC with two private subnets and internet egress.
  - Dedicated ECS security group allowing inbound HTTP on port 8000 (`aws_security_group.app`).
  - ECS Fargate cluster, log group, and service running `nimbus-app`.
  - HTTP API Gateway with `$default` stage auto-deploying proxy routes (`ANY /{proxy+}`, `GET /`, and `POST /requests`).
  - DynamoDB tables (`leases`, `tasks`, `robot_status`, `decision_log`).
  - SQS escalations queue and EventBridge rule.
  - S3 outputs bucket.
  - ECR repository `nimbus-app` with `force_delete = true`.
  - Minimal CloudWatch metric alarm skeleton (`aws_cloudwatch_metric_alarm.missed_checkin`) alerting if edge check-ins fall below threshold within the lease window.
- The Terraform configuration passes `terraform validate` out-of-the-box. Per course instructions, it is designed for evaluation without requiring an active account balance or applying cloud charges.
