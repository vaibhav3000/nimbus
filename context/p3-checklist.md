# P3 status: everything done and not done (2 Nov 2026, end of day, branch `p3-complete`)

**DONE** = built and tested (offline suite of 441 tests; DynamoDB, S3, SQS emulated with moto; Bedrock, IoT and the Knowledge Base stubbed).
**NEEDS YOU** = built, but only you can run or review it (AWS credentials, labels, decisions). **NOT P3** = someone else's.
**Nothing from MS2 onward has run on real AWS or against P2's real edge.** MS1 (14-15 Oct) is the only live run.

## 1. Milestones

| MS | P3 part | Status |
|---|---|---|
| MS1 (18 Oct) | request, clarify, rule check, lease, check-in, OUTPUT_READY | DONE (live on Bedrock + DynamoDB) |
| MS2 (1 Nov) | SC-1, SC-2 (re-plan, hand-over, new version), SC-3, no-LLM fallback | DONE (C1, C3, model-down; both maps) |
| MS3 (6 Nov) | C1/C2/C3 flag; T1-T8 run and plotted | DONE (stand-in model): `results/e3`, `docs/e3-results.md` |
| MS3 | real Bedrock numbers (NFR-1, NFR-4) | **NEEDS YOU**: `docs/aws-verify.md` section 4 |
| MS3 | repo + documentation | DONE except your final README pass |

## 2. Functional requirements (P3 side)

| FR | Status |
|---|---|
| FR-5 stay in lease (cloud side) | DONE |
| FR-8 no duplicates / stale refused | DONE; E6 injector and tests: one effect per message |
| FR-9 three escalations | DONE (CLARIFY answered or relayed; UNEXPECTED_EVENT by agent + rules; OUTPUT_READY per leg, with result summary) |
| FR-11 selective sharing | DONE |
| FR-12 reconnection, silent cluster | DONE cloud side: ticker moves tasks off a silent cluster; heartbeat `leases` reconcile (ok / replaced / stop). The edge must send `leases` (optional for P2) |
| FR-13 understand requests | DONE; the answer is checked against the request text. Accuracy on the DRAFT 50-request set: 100% on the stand-in (labels came from the same templates, so this proves the harness, not the model) |
| FR-14 scheduling, urgent first, deadlines | DONE (+ deadline alarms, timers, retry only when capacity changed) |
| FR-15 checked assignment | DONE (model chooses only when two clusters could take a task; code ranks; checker verifies; rules fallback) |
| FR-16 re-plan and hand-over | DONE |
| FR-17 shared map, plain-language rules | DONE: closures and restrictions durable (file locally, DynamoDB on AWS); survey reports merged; 10-rule draft set 10/10 |
| FR-18 works without Bedrock | DONE; 10-minute forced outage test (`/llm/outage`, `NIMBUS_LLM_OUTAGE`) |
| FR-19 long-term memory, rule retrieval | DONE: ward habits (2 confirmations, never overrides a safety rule); optional Bedrock Knowledge Base with keyword fallback (**NEEDS YOU** to create the KB) |
| FR-20 decision log | DONE (cloud/rule rows; P4 owns the log; plus an in-memory trace with model prompts) |
| FR-22 cancel reaches robots | DONE cloud half (stop lease) |
| FR-1,2,3,4,6,7,10,21 | NOT P3 |

## 3. Non-functional requirements

| NFR | Status |
|---|---|
| NFR-1 request to lease <= 30 s p95 | Measurement built (`/metrics` `request_to_lease_s`); **NEEDS YOU**: a live run on Bedrock. Model calls are now 1 per ordinary request (was 2), and no model call sits inside the global lock |
| NFR-2 recovery after outage | DONE cloud side (health -> drain -> reconcile); the 1-2 min figure is measured with P4/P2 in E4/E5 |
| NFR-3 10 robots, 3 clusters | DONE cloud side: E7 on a generated 3-cluster pack up to 20 robots; decision p95 0.59x the 3-robot baseline; edge half is P2's |
| NFR-4 C1 calls <= 50% of C2 | DONE on the stand-in: ratio 0.288 over T1-T8 (150 vs 520 calls); **NEEDS YOU** to confirm on Bedrock |
| NFR-5 security | DONE: least-privilege IAM, `scripts/audit_iam.py` (passes), edge token on machine routes, no secrets, `scripts/audit_data.py` (synthetic-only). Requester/operator authentication is API Gateway's (P4) |
| NFR-6 byte-identical re-run | DONE: `replay_check.py` (two processes, different hash seeds), E3 re-run, E7 at scale |
| NFR-7 extensibility | DONE: survey (T7) added as a pack template with the lease schema unchanged; three packs plus a generated scale pack |

## 4. AWS and architecture items

| Item | Status |
|---|---|
| Bedrock (Converse, tool use, retry, breaker, cassette) | DONE |
| "Bedrock agent" | Own bounded agent loop (look-ups, every tool call per turn handled, validated final choice, rules fallback). **Not the managed Bedrock Agents service** (a deliberate design choice: the PRD figure shows "Bedrock LLM with tools") |
| Bedrock Knowledge Base | DONE as an option; default keyword retrieval; `scripts/kb_sync.py` prepares the documents |
| DynamoDB: leases, claims, stock, tasks, log | DONE |
| DynamoDB: closures, restrictions, notices, memory, robot status | DONE (`robot_status` table now used) |
| S3: result preview, requester summary, per-task archive, lease bundles | DONE (own bucket only; local folder when no bucket); **NEEDS YOU** to verify on AWS |
| SQS consumer | DONE (at-least-once, poison handling, shared ingest with MQTT); **NEEDS YOU** + P4: IoT rule and dead-letter queue |
| IoT Core publish | DONE (failure never fails a request); **NEEDS YOU** to verify |
| CloudWatch | DONE: EMF metric lines (`NIMBUS_EMF=1`) and `/metrics`; not yet seen in CloudWatch |
| Data and rules with each lease (Q-03) | DONE: `BUNDLE` notice beside each lease (lease schema unchanged) |
| API Gateway, Fargate, VPC, Terraform apply | NOT P3 (P4). P3 supplied IAM and env changes |

## 5. Experiments (P3 share)

| Exp | Status |
|---|---|
| E2 request understanding | Sets (50 / 10 / 20), scorer, manifest DONE; labels are **DRAFTS: NEEDS YOU** to review; real-model scores need a Bedrock run |
| E3 C1/C2/C3 on T1-T8 | DONE on the stand-in (all eight tasks); real-model run **NEEDS YOU** |
| E4 trace replay | NOT P3 |
| E5 Bedrock outage | DONE (clock, env and HTTP injection; 10-minute test) |
| E6 duplicates / stale / re-ordered / byte-identical / audits | DONE (`inject_messages.py`, `replay_check.py`, audits) |
| E7 load and scale | DONE cloud side (`run_e7.py`); real MIMIC timestamps can be plugged in (`--arrivals`) |

## 6. Efficiency problems found in the audit: all fixed

Model calls out of the global lock (a heartbeat no longer waits behind a request); queue retried only when capacity changed (was 50 model calls per 5 idle drains, now 0);
candidate-first allocation (1 model call per ordinary request, was 2); ticker for expiry, deadlines, closures and silent clusters; agent loop answers all tool calls in a turn;
new look-ups and final choices; precomputed re-plan options. Still P4's: the outbox returns its whole file per poll (cursor suggested); one log row per check-in.

## 7. Known limits

- The in-process lease store used locally is single-process; DynamoDB mode is multi-instance safe for leases, but each instance runs its own ticker (notices could repeat; leases cannot double-book).
- A survey uses at most 2 robots per cluster and one lease per cluster.
- The stand-in model proves the plumbing, not model quality; every "stand-in" number is labelled.
- `result_ref` previews read only the cloud's own results bucket.

## 8. Hand-offs

| To | Status |
|---|---|
| P1 | not yet told: node names are read from your files; tell us before changing them (`python scripts/build_sim_topology.py` rebuilds); the robot controller needs a safe `stop` |
| P2 | not yet told: `docs/p2-edge-contract-ms2.md` (stop lease, legs, notice, bundles, `leases` reconcile, edge token, survey reports, `s3://` result refs). P2's branch `feature/robot-edge-connectivity` is unmerged and untested with the cloud |
| P4 | not yet told: `docs/p4-note-ms2.md` (server changes, tests changed, Terraform and env, IoT rule + dead-letter queue, `notice` in `topics.md`) |

## 9. Git

`main` has PR #8. `p3-ms3` (pushed) and `p3-complete` (this branch) add everything after it; one PR from `p3-complete` carries both.
