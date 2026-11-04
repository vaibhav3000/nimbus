# HANDOFF.md: Person 4 slice, for the next agent

**State: complete and green.** 62 P4 tests pass (`python -m pytest tests/test_api.py tests/test_fault_harness.py tests/test_mqtt_transport.py tests/test_cost_backend.py -q`), `terraform validate` passes, SC-1 verified clickable in a real browser (request → clarify → answer → lease v1 → OUTPUT_READY → result, refresh-safe).

## Read first (do not duplicate, they are current)

- README.md: run instructions, real vs stand-in vs Person-3-replace table, API list
- SPEC-P4.md: the spec this was built against
- ASSUMPTIONS.md: every PRD-gap reading chosen (17 numbered)
- REUSE.md: what was reused from the old v3.1 workspace and why
- GLOSSARY.md: the team vocabulary; use these terms
- docs/p4-compliance-matrix.md: complete mapping of FR-8, FR-9, FR-10, FR-20, FR-21, FR-22, NFR-4, NFR-5, NFR-6
- docs/p4-fresh-clone.md: quick start guide for clean machine setup
- docs/e4-e5-evidence.md: fault harness exact reproduction commands

## How to run

```bash
python server.py        # http://127.0.0.1:8000, data in data/
python -m pytest tests/test_api.py tests/test_fault_harness.py tests/test_mqtt_transport.py tests/test_cost_backend.py -q
cd infra/terraform && terraform init -backend=false && terraform validate
```

Terraform binary for this machine lives (gitignored) in `tools/terraform.exe`.

## Architecture & Integration Points

1. **Orchestrator Boundary**: `server.py` interfaces with `cloud.orchestrator.Orchestrator` (Person 3) when available, falling back cleanly to `standin.py` for local offline testing.
2. **Escalations & Re-planning**: `server.py` guards ingress (dedupe, schema validation, stale-version 409 rejection) and delegates re-planning and lease v2 generation to the orchestrator.
3. **Storage & State**: `store.py` provides local atomic file persistence; DynamoDB tables (`leases`, `tasks`, `robot_status`, `decision_log`, `state`) are declared in Terraform with least-privilege IAM policies.
4. **Cost Tracking & Formula**: `cost.py` records raw counters (`COUNTERS`) and provides `calculate_cost_per_task(counters, pricing, num_tasks)` computing the full PRD Section 7 10-term formula with zero-task division guard.
5. **Operator Console & Stop Leases**: `static/operator.html` and `static/operator.js` provide a live spatial floor grid HUD; cancelling a task publishes a stop lease to robots via `orch.stand_down()`.
6. **Network Fault Harness**: `scripts/fault_harness.py` provides a cross-platform userspace `FaultProxy` for testing WAN latency, jitter, packet drops, and partition recovery (E4–E6).

## Operational Details & Boundaries

- Operator cancel publishes a stop lease to robots; intents are preserved in the append-only decision log.
- Network traces in `traces/` (`iisc_corridor.csv`, `iisc_lift.csv`, `iisc_basement.csv`) are short synthetic examples for local testing.
- Cost counters carry stand-in values during local demos; prices are symbolic and passed into `calculate_cost_per_task()`.
- API Gateway HTTP API uses `HTTP_PROXY` integration to forward requests to the ECS container port 8000.
