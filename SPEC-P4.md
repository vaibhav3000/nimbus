# SPEC-P4 — Nimbus Person 4 slice (contracts, requester app, decision log, cloud stand-in, Terraform skeleton, cost hooks)

Source: Nimbus PRD v5.0 (26 Sep 2026), mediator scope order. Glossary: GLOSSARY.md.

## Problem Statement

The team has a PRD but no running vertical slice. Person 3's Bedrock orchestrator is not
built yet, so nobody can demo SC-1 ("Need an infusion pump at 2B-12, urgent.") end to end,
and there is no agreed wire format the edge (Person 2) and cloud (Person 3) work can code
against. The AWS deployment and the per-task cost figure for the report also do not exist.

## Solution

Check four JSON Schemas into `contracts/` as the team's wire contracts. Ship a
single-command local stack: a plain-language requester app (static page) talking to a thin
HTTP API backed by a file store. The API contains a **cloud stand-in** — a fake
orchestrator that parses a request, asks a clarifying question when the stand-in rules say
so, emits a schema-valid lease v1, writes decision-log rows, and reports status — so SC-1
is clickable with no AWS account. `GET /tasks/{id}/timeline` replays any task's full
decision log. A Terraform skeleton (validated, never applied) lays out the real AWS
deployment, and a cost counter records the PRD Sec. 7 formula inputs. Schemas and the
escalation ingress are exposed so Person 3 plugs the real orchestrator in without touching
the app.

## User Stories

1. As ward staff, I want to type a plain-language request, so that I do not need to fill structured forms.
2. As ward staff, I want the system to ask me a clarifying question when my request is vague, so that I get the right pump to the right bed.
3. As ward staff, I want to answer that question in the same app, so that the task continues without another tool.
4. As ward staff, I want to see my task's status at any time, so that I know whether it is waiting, being clarified, leased or done.
5. As ward staff, I want to see the final result of my task, so that I know the delivery actually happened.
6. As ward staff, I want the app to survive a page refresh, so that nothing I sent disappears.
7. As Person 3 (cloud dev), I want the lease, escalation, sub-task and decision-log schemas published, so that my orchestrator emits documents the edge already accepts.
8. As Person 3 (cloud dev), I want a stand-in whose exact ingress routes I inherit, so that I replace fake decisions with Bedrock calls and nothing else changes.
9. As Person 3 (cloud dev), I want repeated message_ids silently ignored, so that my retries and SQS redeliveries cannot double-execute a step.
10. As Person 2 (edge dev), I want the three escalation payload shapes fixed by schema, so that my edge model emits CLARIFY / UNEXPECTED_EVENT / OUTPUT_READY without negotiating formats.
11. As Person 1 (evaluation), I want `GET /tasks/{id}/timeline` to return every decision row, so that I can replay any task's decisions for the report.
12. As Person 1 (evaluation), I want cost counters for every term of the PRD Sec. 7 formula, so that cost per task is computable from a single endpoint.
13. As the mediator/grader, I want the app plus API to boot with one command, so that the demo needs no setup script.
14. As the mediator/grader, I want `terraform validate` to pass without applying anything, so that the infra is reviewable but never touches a real account.
15. As the operator (FR-22 owner), I want the decision log to say who decided (cloud, edge or rule) and under which lease version, so that incidents are attributable.
16. As Person 3 (cloud dev), I want the Terraform skeleton to contain the DynamoDB tables, queues, bus, bucket and IoT topic names, so that my orchestrator's storage targets already exist as names.

## Implementation Decisions

- **Language/stack**: Python 3.14 standard library `http.server` (ThreadingHTTPServer) for
  API + static app serving; `jsonschema` (already installed) for contract validation;
  `pytest` for tests. No web framework, no new dependency.
- **One seam**: all tests go through the public HTTP API and the published schema files.
  No private helpers are imported by tests. The server starts on an ephemeral port in-process.
- **Storage**: file-backed, under `data/` — `tasks.json` (whole task map, rewritten atomically)
  and `decisions.jsonl` (append-only decision log). This is what makes a refresh/restart
  safe; DynamoDB replaces it only in Terraform.
- **Contracts** (`contracts/`, JSON Schema draft 2020-12, house `$id` style inherited from
  the old Nimbus workspace):
  - `lease.schema.json` — required: `lease_id`, `version`, `priority`, `deadline_ts`,
    `expiry_ts`, `allowed_zones`, `forbidden_zones`, `speed_limit`, `assigned_robots`,
    `task_sequence`, `cluster_id`, `when_to_escalate`. `version` >= 1. `priority` in
    {routine, urgent}. `task_sequence` items are robot sub-tasks. Cross-field rule
    `deadline_ts <= expiry_ts` lives in the validation module, not the schema.
  - `escalation.schema.json` — `type` in {CLARIFY, UNEXPECTED_EVENT, OUTPUT_READY},
    `message_id`, `lease_version`, `task_id`, plus a `payload` object whose required keys
    follow PRD Table 2: CLARIFY → `question`, `possible_readings`; UNEXPECTED_EVENT →
    `what_happened`, `where`, `robots_saw`; OUTPUT_READY → `result_ref`.
  - `robot_subtask.schema.json` — `cmd` in {go_to, wait, stop, photo, beep, report},
    optional `robot` and `params`.
  - `decision_log_entry.schema.json` — `who` in {requester, cloud, edge, rule}, `inputs`
    (object), `output` (object), `lease_version` (integer or null), `timestamp`, `task_id`.
- **API surface** (all JSON):
  - `POST /requests` `{text, message_id}` → creates the task; stand-in decides clarify vs
    lease; returns `{task_id, status, question?}`; 201, or 200 with `duplicate: true` when
    `message_id` was seen before (FR-8 shape).
  - `POST /requests/{task_id}/answer` `{text, message_id}` → records the answer, issues
    lease v1; same dedup rule.
  - `POST /escalations` → schema-valid escalation; dedup by `message_id`; escalations
    carrying a `lease_version` lower than the task's current one are dropped as outdated;
    OUTPUT_READY marks the task completed with its `result_ref`.
  - `GET /tasks/{task_id}` → status, request text, question, lease, result.
  - `GET /tasks/{task_id}/timeline` → full ordered decision-log rows (replay).
  - `GET /tasks` → all tasks (the app's status view).
  - `GET /schemas/{name}.json` → the published contracts (Person 3's plug-in point).
  - `GET /cost` → PRD Sec. 7 counters.
- **Cloud stand-in** (`standin.py`, the only file Person 3 must replace): rule-driven.
  SC-1 heuristic: a request naming bed 2B (a paediatric bed) that does not name a pump
  type produces a CLARIFY-style question ("2B-12 is a paediatric bed — do you need a
  paediatric pump?"); the answer (any) resolves it and a lease v1 is issued for the
  East-wing cluster with a 12-minute deadline (issued+12 min), expiry issued+15 min,
  allowed zones [2B], speed limit, a go_to task sequence for one assigned robot, and
  `when_to_escalate` conditions. Non-2B requests lease immediately. Each stand-in decision
  writes decision-log rows: the requester's words as a `requester` row, the parse/lease
  decision as a `cloud` row, and the rule check as a `rule` row.
- **Decision log**: append-only JSONL; rows are only ever appended, never edited; the
  timeline endpoint filters by `task_id` in append order.
- **Cost hooks** (`cost.py`): one function `record(**counters)` persisting named counters
  to `data/cost.json`. The API records every term of the PRD Sec. 7 formula as a stand-in
  value: input/output tokens (len//4 heuristic / 150-token cap), Fargate GB-s, IoT
  messages, DynamoDB ops, SQS sends, S3 ops, data-transfer bytes, API Gateway requests,
  CloudWatch metrics. Prices are NOT applied — the PRD keeps p_* symbolic. No Cost
  Explorer calls.
- **Terraform skeleton** (`infra/terraform/`, AWS provider ~> 5, single region): VPC
  (placeholder: one VPC, two private subnets), ECS Fargate cluster + always-on service
  (no Lambda anywhere, per PRD Fig 4), HTTP API Gateway with a placeholder integration
  (Person 3 replaces with a VPC-link proxy), SQS queue, EventBridge bus + rule routing to
  the queue, DynamoDB tables `leases`, `tasks`, `robot_status`, `decision_log`
  (PAY_PER_REQUEST), S3 bucket for outputs, IoT topic names as locals, and least-privilege
  IAM (task role scoped to the four table ARNs, the one queue, the one bucket prefix,
  Bedrock invoke, IoT connect/publish; execution role for ECR/logs only). `terraform
  validate` must pass; never applied.
- **Repo hygiene**: `data/` and `tools/` are gitignored; `requirements.txt` lists
  jsonschema + pytest only.

## Testing Decisions

- A good test exercises documented, public behaviour: HTTP status codes, JSON bodies, and
  schema validity — the same surface Person 3 and the app see.
- Two test files: `test_contracts.py` (fixtures valid/invalid against the four schemas,
  plus the cross-field rule) and `test_api.py` (live server: SC-1 end to end — request →
  question → answer → schema-valid lease; timeline contains requester + cloud + rule rows;
  repeated `message_id` ignored; failed deliveries don't burn their `message_id`; OUTPUT_READY
  completes the task; schemas served over HTTP; cost counters move). The outdated
  `lease_version` drop is implemented as an ingress guard but is only publicly reachable
  once Person 3's re-planning issues lease v2, so its test lives with Person 3's slice.
- Prior art: the old Nimbus workspace's `tests/test_lease_schema.py` style (schema-fixtures
  in pytest) and its HTTP-free, public-behaviour-first conventions.

## Out of Scope

Gazebo, robot navigation, safety stop, edge SLM, llama.cpp, Bedrock prompts, scheduler,
lease rule checker (real), FR-18 fallback, operator console (FR-22), tc/netem trace harness
(E4–E6), C2/C3 baselines, physical PiCar-X, Needle/E9. Everything Person 2 and Person 3 own.

## Further Notes

- The old `NIMBUS_WORKSPACE` targets PRD v3.1 (corridor/epoch lease, L0–L3 ladder); its
  schemas do not match v5.0 and are not reused as documents. What IS reused: schema house
  style, the jsonschema/pytest dependency set, and the cross-field-check-outside-JSON-Schema
  pattern. See REUSE.md.
- All PRD details the scope leaves open are recorded with their chosen reading in
  ASSUMPTIONS.md.
