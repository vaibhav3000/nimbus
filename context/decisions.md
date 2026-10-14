# Recorded Decisions and PRD Interpretations

This document summarizes the 17 design decisions and interpretations of PRD v5.0 ambiguities recorded in `ASSUMPTIONS.md`, along with the specific files where each decision is implemented.

### 1. Legacy Workspace vs PRD v5.0
The previous workspace (`Projects/NIMBUS_WORKSPACE`) implemented PRD v3.1 (corridor and epoch leases). Its schemas could not be used directly for PRD v5.0. Only structural patterns were carried over: JSON Schema house style, the jsonschema/pytest toolchain, and semantic cross-field checking outside JSON Schema.
- Files: `contracts/`, `contracts.py`, `REUSE.md`

### 2. Workspace and Git Repository Location
Work was initialized in `Projects/nimbus-p4/` with its own clean Git history. This enables precise code review diffs and allows independent team integration via pull requests.
- Files: `.gitignore`, repository root

### 3. Lease Timing and Authority Invariant
Timestamps follow ISO 8601 UTC. `deadline_ts` is set to issue time plus 12 minutes for urgent tasks (60 minutes for routine). `expiry_ts` is set to deadline plus 3 minutes, ensuring authority outlives the task deadline. The invariant `deadline_ts <= expiry_ts` is enforced in code rather than in JSON Schema.
- Files: `contracts.py`, `standin.py`

### 4. Priority Enumeration
The PRD states urgent tasks take precedence without fixing an enum. The simplest binary model was selected: `["routine", "urgent"]`.
- Files: `contracts/lease.schema.json`

### 5. Escalation Trigger Conditions (`when_to_escalate`)
The PRD requirement that leases specify when to ask the cloud is implemented as an array of structured condition strings: `["door_closed", "deadline_at_risk", "robot_failed"]`.
- Files: `contracts/lease.schema.json`, `standin.py`

### 6. Inclusion of Requester in Decision Log
FR-20 designates decision sources as cloud, edge, or rule. To allow complete replay in `GET /tasks/{id}/timeline`, human input is captured as a `requester` row within the same schema. Only cloud, edge, and rule rows represent automated decisions.
- Files: `contracts/decision_log_entry.schema.json`, `server.py`, `store.py`

### 7. Clarification Heuristic (SC-1 Stand-in)
Because no LLM runs in this slice, requests targeting bed 2B-12 without specifying a pump trigger a clarification question ("2B-12 is a paediatric bed. Do you need a paediatric pump?") with options `["paediatric infusion pump", "standard infusion pump"]`. Answering resolves the task and issues lease v1.
- Files: `standin.py`, `server.py`

### 8. Lease v1 Content for SC-1
Lease v1 assigns the east-wing cluster, marks priority urgent, targets zone `2B-12`, assigns robot `R1`, specifies a `go_to` and `beep` sequence, caps speed at 0.8 m/s, and sets standard escalation conditions.
- Files: `standin.py`

### 9. Edge Escalation Ingress Route
`POST /escalations` exists in this slice so that `OUTPUT_READY` can mark tasks complete in the requester UI, FR-8 deduplication and stale version drops can be validated over HTTP, and Person 3 receives a tested route ready for Bedrock re-planning.
- Files: `server.py`

### 10. Cost Counter Accounting
`cost.py` records symbolic counters for all terms in PRD Sec. 7: input tokens (`len(text)//4`), output tokens (150 per cloud decision), Fargate compute (0.001 GB-s), IoT publications (1 per lease), DynamoDB writes, SQS messages, and S3 results. Prices stay symbolic per PRD; no AWS Cost Explorer calls are made.
- Files: `cost.py`, `server.py`

### 11. API Gateway Mock Integration
A full API Gateway to ECS Fargate connection requires an internal ALB or VPC Link, which exceeds a validation skeleton. A mock integration is configured with a clear replacement comment.
- Files: `infra/terraform/main.tf`

### 12. Terraform Validation Tooling
A portable Terraform binary was used in `tools/` (gitignored) to guarantee that `terraform validate` passes honestly. The provider lockfile is committed.
- Files: `infra/terraform/.terraform.lock.hcl`

### 13. Decision Log Ordering
Rows are appended to `data/decisions.jsonl` in causal order. The timeline endpoint returns them in append order, avoiding unnecessary sequence number schemes.
- Files: `store.py`, `server.py`

### 14. Message Deduplication Semantics (FR-8)
A message ID is only recorded as seen after the request is successfully validated and accepted. If a request fails validation or task lookup, its `message_id` is not burned, allowing retries to succeed.
- Files: `server.py`

### 15. Requester App Demo Completion Trigger
Because the physical edge cluster is not present, the requester UI includes a labelled demo button that posts an `OUTPUT_READY` escalation to demonstrate task completion.
- Files: `static/index.html`, `static/app.js`

### 16. Scoped IAM Permissions
The task role policy adds `events:PutEvents` (for EventBridge publishing) and `iot:Subscribe` (for edge check-in consumption), both scoped to specific resource ARNs under least-privilege principles.
- Files: `infra/terraform/main.tf`

### 17. Pre-Handoff Code Review Fixes
All findings from the standards and specification review were resolved: removed duplicate escalation responses, made task ID allocation thread-safe with a monotonic counter, marked deduplication only on acceptance, consolidated decision row formatting, escaped UI HTML interpolation, and aligned test assertions with public interfaces.
- Files: `server.py`, `store.py`, `static/app.js`, `tests/test_api.py`
