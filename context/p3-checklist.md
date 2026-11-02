# P3 checklist: milestones, FR, NFR (as of 2 Nov 2026)

Legend: DONE = built and tested. PART = partly built. TODO = not built. N/A = owned by someone else (named).
"Tested" means the offline suite (255 tests) and/or emulated DynamoDB. Only MS1 has been run on real AWS.

## Milestones

| MS | Date | P3's part | Status |
|---|---|---|---|
| MS1 | 18 Oct | Request -> clarify -> rule check -> lease -> check-in -> OUTPUT_READY (T1) | **DONE**, shown live on Bedrock + DynamoDB |
| MS2 | 1 Nov | SC-1 (vague urgent request) | **DONE** |
|     |       | SC-2 (blocked corridor, re-plan, cross-cluster hand-over) | **PART**: re-plan v2 works; hand-over TODO |
|     |       | SC-3 ("Ward 2C is now isolation": hold routine, admit urgent, move pumps out) | **PART**: restriction engine works; the sentence -> restriction and extraction tasks TODO |
|     |       | No-LLM fallback (FR-18) | **DONE** |
| MS3 | 6 Nov | C1/C2/C3 flags | **DONE** (C2 relay added) |
|     |       | T1-T8 run and plotted | **PART**: T1-T4, T6, T8 run; T5 (needs hand-over) and T7 (needs survey template) TODO; T6 only holds/admits, in-flight recall TODO |
|     |       | Real (Bedrock) numbers | **TODO**: current E3 numbers use the offline stand-in model |
|     |       | Repo + docs submitted | **PART**: `startup.md`, `context/` updated; final README pass TODO |

So MS1 is complete; MS2 is **not** complete (SC-2 hand-over and SC-3 language are missing).

## Functional requirements (cloud side, P3 only)

| FR | What | Status |
|---|---|---|
| FR-5 | Stay within the lease (cloud side: zones, expiry, version in the lease) | DONE |
| FR-8 | No duplicate actions: message ids, versions, stale refused | DONE (cloud side) |
| FR-9 | Handle the 3 escalations | PART: OUTPUT_READY done; UNEXPECTED_EVENT only avoids named zones; CLARIFY from the edge not answered by the LLM |
| FR-11 | Selective sharing (only affected clusters told) | TODO |
| FR-12 | Reconnection: reconcile and renew/replace leases; no robot under two leases | PART: one-holder-per-robot transaction done; edge summary reconcile and silent-cluster monitor TODO |
| FR-13 | Understand requests (ask when unsure) | PART: one delivery per message; several items dropped. 50-request accuracy test (E2) TODO |
| FR-14 | Scheduling: urgent first, deadlines | DONE |
| FR-15 | Checked assignment, rule fallback after 2 failures | DONE |
| FR-16 | Re-plan and cross-cluster hand-over | PART: re-plan done; hand-over TODO; re-plan from the robot's current zone TODO |
| FR-17 | Shared map, plain-language rules | PART: runtime restrictions work; merging robot reports and sentence -> rule TODO |
| FR-18 | Works without Bedrock | DONE (circuit breaker, rules path, "reduced autonomy" notice) |
| FR-19 | Long-term memory as context | TODO (per-task log only) |
| FR-20 | Decision log (cloud rows) | DONE (P4 owns the log; P3 writes cloud/rule rows) |
| FR-3, 4, 6, 7, 10 | Edge supervision, local handling, outages, takeover, store-and-forward | N/A: P2 |
| FR-1, 2 | Safety stop, navigation | N/A: P1 |
| FR-21, 22 | Requester app, operator console | N/A: P4 (FR-22 cancel reaching robots needs a P3 stand-down lease: TODO) |

## Non-functional requirements

| NFR | Target | P3 status |
|---|---|---|
| NFR-1 | Request -> lease <= 30 s p95 | DONE at MS1: about 2-3 s per LLM step. Needs a re-measure on the team account |
| NFR-2 | Recovery after outage | PART: leases expire and renew correctly; reconnection reconcile TODO (E4/E5, shared) |
| NFR-3 | 10 robots across 3 clusters | TODO (E7); packs have 6 robots in 2 clusters |
| NFR-4 | C1 model calls <= 50% of C2 | PART: 0.478 on the stand-in model (66 vs 138 calls); needs a real Bedrock run |
| NFR-5 | Least-privilege IAM, no secrets, auth | PART: `p3.tf` and `infra/iam/` done; IAM audit and authenticated access TODO |
| NFR-6 | Byte-identical re-run | PART: E3 re-run verified identical; cassette replay for Bedrock exists; full E6 diff of two decision logs TODO |
| NFR-7 | New task/robot type without changing the lease schema | DONE for packs (hospital and rescue); `rescue` proves it |

## Experiments (P3's share)

| Exp | What | Status |
|---|---|---|
| E2 | Cloud half: 50 requests (a quarter vague), 10 rules, 20 context-dependent | TODO (you write the gold labels) |
| E3 | C1/C2/C3 on T1-T8 | PART (see MS3 above) |
| E5 | Bedrock outage | PART: fallback built; the 10-minute forced-failure run TODO |
| E6 | Repeated/outdated/re-ordered messages, byte-identical re-run | PART |
| E7 | Load and cost with 3 -> 10+ robots | TODO (after MS3) |

## Other P3 TODO

- SQS consumer and a tested IoT Core publish (code is P3, deployment is P4).
- Review findings not yet fixed: re-plan route from the robot's current zone; stock reservation leak on a non-Conflict commit error.
- Hand to P4 (their files): Fargate env vars for `NIMBUS_*`, the cost counter `llm_calls`, and the check-in/control guards in `server.py`.
