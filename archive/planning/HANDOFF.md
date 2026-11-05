# HANDOFF.md: Person 4 slice & integrated architecture

**State: complete, integrated, and green.** Over 400 tests pass across the repository (58 P4 tests, 51 edge tests, 20 simulation tests), `terraform validate` passes honestly, and SC-1 is verified clickable in a real browser end-to-end (request → clarify → answer → lease v1 → check-in/renewal → pause/cancel stop lease → OUTPUT_READY → result).

## Architecture & Current Status (PRD v5.0)

- **Contracts (`contracts/`)**: Published wire schemas for `lease`, `escalation`, `robot_subtask`, `decision_log_entry`, and topic topologies (`contracts/topics.md`).
- **Cloud Orchestration & Fallback (FR-13..FR-18)**: Bedrock LLM orchestrator with deterministic rule-checker verification. Full FR-18 circuit breaker fallback to rule-based allocation (C3 mode) when Bedrock is unreachable.
- **Operator Console (FR-22)**: Interactive 3D spatial floor map (`static/operator.html`, `static/operator.js`), fault telemetry badge, and pause/cancel controls. When an operator triggers pause or cancel, `server.py` calls `orch.stand_down()`, which generates and publishes a schema-valid **stop lease** (`[{"cmd": "stop"}]`, incremented version) delivered to edge robots via `nimbus/{cluster_id}/lease` and `data/outbox.jsonl`.
- **Requester App (FR-21)**: Plain-language interface at `/` for staff requests with interactive clarification dialogs.
- **Decision Log (FR-20)**: Append-only JSONL decision log (`store.py`) replayable via `GET /tasks/{id}/timeline`.
- **Cost Hooks (PRD Sec. 7)**: 10 raw cost counters in `cost.py` exposed via `GET /cost`.
- **Cloud-Edge Transport**: Switchable between default local HTTP + JSONL and MQTT on the five canonical topics via `NIMBUS_TRANSPORT=mqtt`.
- **Infrastructure (`infra/terraform/`)**: Complete validated skeleton including VPC, dedicated ECS security group, Fargate service, API Gateway with auto-deploying `$default` stage, DynamoDB tables, SQS escalations queue, EventBridge bus, S3 bucket, ECR repository with `force_delete`, and CloudWatch alarm skeleton.

## How to run

```bash
python server.py                                # http://127.0.0.1:8000
python -m pytest tests/test_api.py tests/test_fault_harness.py tests/test_mqtt_transport.py tests/test_cost_backend.py -q
cd infra/terraform && terraform validate
```

## Security & Operational Flags

- `NIMBUS_DEMO_FAULTS`: Set to `1` to enable local demo access to `/fault` and `/llm/outage` diagnostic endpoints (disabled by default in production).
- `NIMBUS_ALLOW_UNKNOWN`: Defaults to `0` (off). Rejects heartbeats or check-ins from unregistered cluster IDs with HTTP 404 unless enabled.
- `NIMBUS_EDGE_TOKEN`: When set, protects edge endpoints with Bearer token authentication (NFR-5).
- `NIMBUS_TRANSPORT`: Set to `mqtt` to activate MQTT transport (defaults to `http`).
