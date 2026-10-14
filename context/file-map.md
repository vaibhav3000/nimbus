# Repository File Map

This document catalogues every important file in the `nimbus-p4` repository and its specific responsibility.

## Documentation and Specifications

- `README.md`: Project overview, quickstart instructions, boundary status table, API route listing, and cost accounting summary.
- `SPEC-P4.md`: Comprehensive specification for the Person 4 slice, user stories, architectural decisions, test strategy, and scope boundaries.
- `ASSUMPTIONS.md`: 17 documented interpretations of PRD v5.0 ambiguities and gaps.
- `HANDOFF.md`: Handoff documentation detailing current status, test commands, known constraints, and guidance for Person 3.
- `GLOSSARY.md`: Canonical domain terms, forbidden aliases, and authority definitions across Nimbus.
- `REUSE.md`: Audit of concepts, schemas, and configurations carried over from earlier project artifacts versus newly authored code.

## Core Python Application

- `server.py`: Standard library HTTP server (`ThreadingHTTPServer`) exposing the cloud REST API, ingress routes for staff and edge clusters, schema endpoints, and static asset serving.
- `contracts.py`: JSON Schema loader and validator enforcing wire contracts and semantic invariants (such as `deadline_ts <= expiry_ts`).
- `standin.py`: Rule-based cloud stand-in simulating SC-1 clarification and lease generation; designed for direct replacement by Person 3.
- `store.py`: File-backed thread-safe persistence layer managing task state (`tasks.json`), the append-only decision log (`decisions.jsonl`), and the lease outbox (`outbox.jsonl`).
- `cost.py`: Accounting module tracking each metric term of the PRD Section 7 cost equation (`cost.json`).

## Wire Contracts (`contracts/`)

- `contracts/lease.schema.json`: JSON Schema (Draft 2020-12) defining the cloud-to-edge authority lease contract.
- `contracts/escalation.schema.json`: JSON Schema defining the edge-to-cloud escalation contract (`CLARIFY`, `UNEXPECTED_EVENT`, `OUTPUT_READY`).
- `contracts/robot_subtask.schema.json`: JSON Schema defining primitive robot commands (`go_to`, `wait`, `stop`, `photo`, `beep`, `report`).
- `contracts/decision_log_entry.schema.json`: JSON Schema for append-only audit trail rows (`requester`, `cloud`, `edge`, `rule`).
- `contracts/topics.md`: Topic naming specifications for cloud-edge pub/sub messaging over AWS IoT Core.

## Requester and Operator Web Interfaces (`static/`)

- `static/index.html`: Requester web application interface (FR-21) allowing ward staff to submit plain-language requests and answer clarifications.
- `static/app.js`: Requester frontend client logic handling API communication, timeline polling, and demo completion simulation.
- `static/operator.html`: Minimal operator console interface (FR-22 slice) displaying tasks and decision timelines.
- `static/operator.js`: Operator frontend client logic for inspecting tasks and logging pause/cancel operator intents.

## Infrastructure as Code (`infra/terraform/`)

- `infra/terraform/main.tf`: AWS infrastructure definitions including VPC, ECS Fargate cluster/service, DynamoDB tables, SQS queues, EventBridge bus, S3 bucket, and IAM roles.
- `infra/terraform/variables.tf`: Input variable definitions for region, environment, and container image names.
- `infra/terraform/outputs.tf`: Exported infrastructure resource identifiers and table names.
- `infra/terraform/.terraform.lock.hcl`: Provider version lockfile ensuring deterministic syntax validation.

## Automated Test Suite (`tests/`)

- `tests/test_api.py`: End-to-end integration tests exercising all HTTP routes, message deduplication, cost tracking, and static UIs.
- `tests/test_contracts.py`: Unit tests validating sample valid and invalid payloads against each schema in `contracts/`.
- `tests/conftest.py`: Pytest configuration setting up an isolated temporary server and storage directory for test runs.

## Project Configuration

- `requirements.txt`: Python package dependencies (`jsonschema` and `pytest`).
- `pyproject.toml`: Pytest configuration defining test discovery paths.
- `.gitignore`: Git exclusion patterns for local data directories, tools, caches, and compiled bytecode.

## Cloud orchestrator, Person 3 (`cloud/`, `packs/`, `scripts/`)

- `cloud/`: domain-agnostic brain that replaces `standin.py` (pipeline: health check, context, intent, allocation, rule checker, conditional lease commit, re-plan, renewal, rules-only fallback). Includes `llm.py` (Bedrock Converse adapter, circuit breaker, record/replay) and `aws_dynamo.py` / `aws_store.py` (DynamoDB backends, used when `NIMBUS_BACKEND=aws`).
- `packs/`: domain data (`hospital`, `rescue`): topology, fleet, policies, prompts, scenarios, validated by `packs/pack.schema.json`. No domain words live in `cloud/`.
- `scripts/`: `edge_sim.py` (fake edge cluster), `bedrock_hello.py`, `smoke_live.py` (live AWS checks, run by a human with credentials).
- `infra/terraform/p3.tf`, `infra/iam/p3-dev-policy.json`: orchestrator state table, egress, task-role permissions, budget alarm; scoped dev IAM policy. Validated only, never applied.
- `context/p3-cloud.md`: how the cloud decision plane works, how to run it, settings and AWS notes.
- `requirements-aws.txt`: boto3 and moto, optional (only for the AWS backend and its emulated tests).

## Agent Context (`context/`)

- `context/README.md`: Primary onboarding guide for future agents working in this repository.
- `context/status.yaml`: Structured status breakdown across done, stand-in, waiting, and out-of-scope work.
- `context/architecture.md`: Detailed tier breakdown and implementation boundary mapping.
- `context/decisions.md`: Shortened summary of the 17 recorded decisions with file links.
- `context/file-map.md`: This file, documenting each component's location and purpose.
- `context/team.yaml`: Team member roles, track boundaries, and pull request merge workflow.
- `context/agent-instructions.md`: Core behavioral rules and constraints for future automated agents.
