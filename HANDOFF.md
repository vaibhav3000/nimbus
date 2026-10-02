# HANDOFF.md — Person 4 slice, for the next agent

**State: complete and green.** 28 tests pass, `terraform validate` passes, SC-1
verified clickable in a real browser (request → clarify → answer → lease v1 →
OUTPUT_READY → result, refresh-safe). Code review (Standards + Spec axes) done;
all findings fixed. Work is committed on `main` (7 ticket commits + review fix).

## Read first (do not duplicate, they are current)

- README.md — run instructions, real vs stand-in vs Person-3-replace table, API list
- SPEC-P4.md — the spec this was built against
- ASSUMPTIONS.md — every PRD-gap reading chosen (17 numbered)
- REUSE.md — what was reused from the old v3.1 workspace and why
- GLOSSARY.md — the team vocabulary; use these terms
- .scratch/nimbus-p4/issues/ — the six tickets, all closed

## How to run

```bash
python server.py        # http://127.0.0.1:8000, data in data/
python -m pytest tests/ -q
cd infra/terraform && terraform init -backend=false && terraform validate
```

Terraform binary for this machine lives (gitignored) in `tools/terraform.exe`.

## What the next agent (Person 3) plugs in where

1. **standin.py** — replace entirely with the Bedrock orchestrator + scheduler +
   rule checker. Keep the module boundary: server.py calls
   `wants_clarification / clarifying_question / build_lease / now_iso`.
2. **server.py `_post_escalation`** — keep route + guards (dedupe, schema,
   stale-version 409, log rows); replace the "record only" reaction with real
   re-planning. The stale-version guard has no public test until a lease v2
   path exists — write it when re-planning lands.
3. **store.py** — swap for DynamoDB (tables exist in Terraform: leases, tasks,
   robot_status, decision_log with task_id+ts keys). `next_task_id` /
   `put_task` / `append_decision` / `timeline` are the callsites.
4. **cost.py callers** — feed real Bedrock usage (input/output tokens) into
   `record()`; counter names are the PRD Sec. 7 terms.
5. **Terraform** — replace the API Gateway MOCK integration with a VPC-link
   proxy; set `app_image`; add VPC endpoints/NAT. Never apply without the
   account owner.

## Known limits (deliberate, see ASSUMPTIONS for the why)

- The stand-in issues only lease v1; no re-planning, no v2, no FR-18 fallback.
- Escalations CLARIFY/UNEXPECTED_EVENT are recorded, not acted on.
- Operator pause/cancel are recorded intents only; delivering them to robots
  (and a real FR-22 console) is the next slice.
- FR-8 dedupe is check-then-mark (mark on acceptance), so two identical
  concurrent deliveries can both pass; Person 3's real ingress (SQS/IoT) must
  make the claim atomic. Task status writes are last-writer-wins under
  concurrent ingresses for the same reason.
- Cost counters carry stand-in values; prices are never applied.
- The app's "Demo: edge sends OUTPUT_READY" button is labelled demo glue for
  Person 2's edge.

## Suggested skills

- `tdd` — any behaviour change in server.py/store.py (public HTTP seam only)
- `code-review` — re-run on the next diff against this commit
- `ponytail` (full) — still the house discipline for this repo
