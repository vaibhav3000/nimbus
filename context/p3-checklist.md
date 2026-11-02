# P3 status: everything done and not done (2 Nov 2026, evening)

**DONE** = built and tested offline. **PART** = partly built. **NOT DONE** = not built. **N/A** = someone else's.
"Tested" means the offline suite (347 tests) and the emulated-DynamoDB run. **Only MS1 (14-15 Oct) has ever run on real AWS.**
Nothing from MS2 has been tried against P2's real edge or on real Bedrock/IoT/SQS/S3 (one manual Bedrock demo run by the user only).

## 1. Milestones

| MS | Date | P3 part | Status |
|---|---|---|---|
| MS1 | 18 Oct | request, clarify, rule check, lease, check-in, OUTPUT_READY (T1) | **DONE**, shown live on Bedrock + DynamoDB |
| MS2 | 1 Nov | SC-1 vague urgent request | **DONE** (C1, C3, model-down) |
|     |       | SC-2 corridor blocked, gate closed, re-plan, hand-over, new lease version | **DONE** cloud side, both maps |
|     |       | SC-3 "Ward 2C is now isolation..." | **DONE** (restriction, recall, extraction tasks, notices) |
|     |       | no-LLM fallback (FR-18) | **DONE** |
| MS3 | 6 Nov | C1/C2/C3 flag | **DONE** |
|     |       | T1-T8 on all three, plotted | **PART**: T1-T4, T6, T8 run (stand-in model, hand-made map). T5 and T7 not in the runner. T6 recall not in the runner. |
|     |       | real Bedrock numbers (NFR-1, NFR-4) | **NOT DONE** (needs a recorded run on AWS) |
|     |       | repo + docs submitted | **PART** (startup.md, context, plans, handoff notes exist; final README and results doc not written) |

## 2. Functional requirements (P3 side)

| FR | Status | Note |
|---|---|---|
| FR-5 stay in lease (cloud side) | DONE | zones, expiry, version in every lease |
| FR-8 no duplicates, stale refused | DONE | message ids, 409 with current version |
| FR-9 three escalations | DONE | OUTPUT_READY (per leg), UNEXPECTED_EVENT, CLARIFY |
| FR-11 selective sharing | DONE | MAP / WARNING notices only to clusters whose area or live leases include the zone |
| FR-12 reconnection, silent cluster | **NOT DONE** | no monitor, no reconcile summary; leases simply expire |
| FR-13 understand requests | PART | works; several items per message work; 50-request accuracy (E2) not measured |
| FR-14 scheduling, urgent first | DONE | no timers (see section 6) |
| FR-15 checked assignment | DONE | model proposes, Rule Checker disposes, rules fallback |
| FR-16 re-plan and hand-over | DONE | re-plan from the robot's zone, legs, re-assignment; 30 s budget not measured |
| FR-17 shared map, plain-language rules | PART | closures from reports and sentence-to-rule work; **not durable** (memory only); 10-rule test not run |
| FR-18 works without Bedrock | DONE | circuit breaker, rules path, "reduced autonomy" notice |
| FR-19 long-term memory, rule knowledge retrieval | **NOT DONE** | rules are keyword-retrieved into the prompt; no Knowledge Base; no memory |
| FR-20 decision log | DONE | cloud/rule rows; P4 owns the log; plus a separate in-memory trace (new) |
| FR-22 cancel reaches robots | DONE (cloud half) | stop lease; P2 must honour it |
| FR-1,2,3,4,6,7,10 | N/A | P1 / P2 |
| FR-21, FR-22 UI | N/A | P4 (P3 added a separate /map page) |

## 3. Non-functional requirements

| NFR | Status |
|---|---|
| NFR-1 request to lease <= 30 s p95 | **NOT re-measured**. MS1 showed 2-3 s per model call, so about 5-8 s. Not measured under load; one request currently blocks all others (see section 6). |
| NFR-2 recovery after outage | PART: leases expire/renew correctly; no reconnect reconcile |
| NFR-3 10 robots, 3 clusters | **NOT DONE** (maps have 2 clusters, 1-3 robots each) |
| NFR-4 C1 calls <= 50% of C2 | PART: 0.478 on the stand-in model; **no real Bedrock measurement** |
| NFR-5 security | PART: least-privilege policy and budget alarm exist (`p3.tf`, `infra/iam/`); **no IAM audit, no API authentication** |
| NFR-6 byte-identical re-run | PART: E3 re-run verified identical; cassette replay exists; no decision-log diff tool |
| NFR-7 extensibility | DONE for packs (hospital, hospital_sim, rescue); new task types are templates, lease schema unchanged |

## 4. AWS and architecture items (PRD Sec. 6) as they touch P3

| Item | Status |
|---|---|
| Bedrock (Converse, tool use, retry, circuit breaker, cassette) | DONE; used live in MS1 and in one user demo run (Nova Lite, apac profile, ap-south-1) |
| "Bedrock agent" | PART: **own bounded agent loop** (look-up tools, validated final choice), **not the managed Bedrock Agents service** |
| Bedrock Knowledge Base for rules (DEC-11) | **NOT DONE**; decision still open (prompt vs KB) |
| DynamoDB: leases, claims, stock, tasks, log | DONE (moto-tested; MS1 live). `robot_status` table unused |
| DynamoDB: closures, restrictions, notices, place inventory durability | **NOT DONE** for closures/restrictions/notices (memory); place inventory is durable |
| S3 (outputs, logs, maps) | **NOT DONE**: the cloud stores the `result_ref` string only; never reads or writes S3; no result summary |
| SQS consumer for edge messages | **NOT DONE** (queue exists in Terraform, P4's) |
| IoT Core publish to edge | PART: code in `cloud/aws_store.py`, **never run on real AWS**; P2's edge does not use MQTT |
| API Gateway / Fargate / VPC / Terraform apply | N/A: P4. P3 added the `NIMBUS_*` env vars to the task definition (needs P4 review, merged in PR #8) |
| CloudWatch metrics | **NOT DONE** (counters only, via P4's cost book) |
| Container image (`Dockerfile`) | DONE (PR #2) |
| "Data and rules the edge needs travel with each lease" (Q-03) | **NOT DONE** |

## 5. Experiments (P3 share)

| Exp | Status |
|---|---|
| E1 edge model | N/A (P2; P2's results exist) |
| E2 request understanding (50 requests, 10 rules, 20 context) | **NOT DONE**: no labelled sets, no scorer |
| E3 C1/C2/C3 on T1-T8 | PART (stand-in model; T5, T7 missing) |
| E4 trace replay | N/A (P4 harness exists in `scripts/fault_harness.py`; P3 not involved) |
| E5 Bedrock outage 10 min | PART: fallback built and tested; no 10-minute forced-failure run |
| E6 duplicates / stale / re-ordered / byte-identical | PART: unit tests for the cloud side; no injector tool, no log diff |
| E7 load and cost | NOT DONE |

## 6. Known problems found in the audit (not yet fixed)

| Problem | Measured | Fix planned |
|---|---|---|
| Model calls run inside the server's global lock | an edge heartbeat waited 1.84 s behind one request with a 1 s model | plan outside the lock, commit inside |
| Idle queue retries spend model calls on every heartbeat | 50 calls in 5 drains with 5 queued tasks | retry only when capacity changed; skip the model if no candidate |
| Allocation call rarely has a choice | 33 of 33 allocation calls had at most one candidate | candidate-first allocation |
| No timers | a task stays `leased` after its lease expires; queued tasks never time out | one `tick()` plus thread |
| `GET /outbox` returns the whole file | 7.5 MB per poll at 5000 rows | cursor (P4's `store.py`) |
| Agent loop reads only the first tool call per turn | confirmed in code | handle all calls |
| Closures, restrictions, notices lost on restart | by design today | durable rows |

## 7. What P3 built (tools and files)

- `cloud/`: orchestrator, Rule Checker, allocator, lease repos (memory and DynamoDB), fleet registry, scheduler, packs loader, mapstate, legs, react, statements, notify, agent loop, read tools, trace, map view, LLM layer (Bedrock, stand-in, circuit breaker, cassette).
- Packs: `hospital` (hand-made map), `hospital_sim` (P1's Gazebo map, **now the default**), `rescue`.
- Scripts: `demo_ms2.py` (narrated scenarios), `trace.py` (message ladder), `edge_sim.py` (scripted fake edge, `--pace` for real-time driving), `run_e3.py` (C1/C2/C3 runner), `smoke_live.py`, `bedrock_hello.py`, `build_sim_topology.py`.
- Pages and endpoints: `/map` (live map, answers questions, conversation, replay), `/map.json`, `/trace`, `/tasks/{id}/trace`, `/notifications`, `/relay/decide` (C2), `/fleet/stock`, `/map/reopen`.
- Docs: `docs/p2-edge-contract-ms2.md`, `docs/p4-note-ms2.md`, plans for MS2 and MS3 under `docs/superpowers/plans/`.

## 8. Hand-offs to other people

| To | What they need to do | Status |
|---|---|---|
| P1 | tell us before changing node names or edges (we rebuild the pack); robot controller needs a safe `stop` | not yet told |
| P2 | handle the stop lease, legs, optional `notice` topic; send robot zones as map node ids; use `hospital_sim` ids. P2's newer branch `feature/robot-edge-connectivity` (30 Oct, "not finalised": tactical autonomy, sim-robot adapter, outbox resilience) is **not merged and not tested with the cloud** | not yet told |
| P4 | PR #8 is **merged into main** without the contract addendum; still to do: add `notice` to `contracts/topics.md` and the IoT rule, review of `server.py` additions, outbox cursor, fewer check-in rows, link `/map`, own SQS consumer and S3 uploads | not yet told |

## 9. Git state

- `main` (origin) includes PR #8 (MS2 cloud work, C2 mode, E3 runner, demo).
- `p3-ms3` (this branch) adds: `hospital_sim` default, `/map` page, trace and CLI, paced fake edge, stand-in clarification fix, MS3 plan, this list. Not yet a PR.
- Older P3 branches (`p3-cloud-brain`, `p3-dockerfile-readme`, `p3-p1-topology`, `p3-ms2`) are all merged.
