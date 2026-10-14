# Instructions for Future Agents

If you are an AI coding assistant working in this repository, you must adhere strictly to the rules and operating principles outlined below.

## 1. Read Context First
Before editing or creating code in this repository:
- Read `context/README.md`, `context/status.yaml`, `context/architecture.md`, `context/decisions.md`, and `context/file-map.md`.
- Familiarize yourself with domain terms in `GLOSSARY.md` and recorded PRD interpretations in `ASSUMPTIONS.md`.

## 2. Do Not Rewrite Contracts Without a Team Check
- The schemas in `contracts/` (`lease.schema.json`, `escalation.schema.json`, `robot_subtask.schema.json`, `decision_log_entry.schema.json`) and topic definitions in `contracts/topics.md` represent the binding wire protocol for the entire Nimbus team.
- Edge (Person 2), Cloud (Person 3), and Simulation (Person 1) rely directly on these shapes.
- Never modify schema properties, required keys, or enums unilaterally without an explicit team review.

## 3. Do Not Implement Other People's Tracks
Keep strictly within scope boundaries:
- Person 1 owns simulation (Gazebo, robot world coordinates, evaluation report replay).
- Person 2 owns the edge cluster (edge SLM, local supervisor, lease execution, hardware drivers).
- Person 3 owns the cloud orchestrator (Bedrock integration, task decomposition, scheduler, rule checker, and FR-18 reduced-autonomy fallback).
- Person 4 (this repo) owns the shared foundation, wire contracts, requester UI, decision log, API ingress, and Terraform skeleton.
- Do not build mock LLMs, Gazebo physics, or edge SLM runtimes into this codebase unless specifically instructed.

## 4. Ponytail Discipline (Full)
Maintain the ponytail engineering mindset at all times:
- The best code is code that does not need to be written (YAGNI).
- Standard library first: prefer Python stdlib (`http.server`, `urllib`, `dataclasses`, `pathlib`) over adding third-party frameworks.
- Zero unnecessary dependencies: do not add web frameworks (FastAPI, Flask, Django) or heavy libraries when standard library tools suffice.
- Shortest clean diff: write minimal, direct, maintainable code.
- Avoid speculative abstractions: no factories for single products, no interfaces for single implementations, and no boilerplate scaffolding for hypothetical future needs.
- If you make a deliberate shortcut, mark it with a `# ponytail: [ceiling and upgrade path]` comment.

## 5. Test Through HTTP Only
- All tests must test public system behavior through the public HTTP API and published JSON Schemas.
- Never import private internal functions or server helper classes directly into test files.
- Testing the external HTTP seam ensures that Person 3 can swap `standin.py` for Bedrock or `store.py` for DynamoDB without modifying test assertions.

## 6. Infrastructure Safety (Never Apply)
- `infra/terraform/` is a validation skeleton representing the PRD Fig 4 cloud architecture.
- Any infrastructure modifications must pass `terraform validate` cleanly.
- Never execute `terraform apply` or touch live cloud infrastructure without explicit authorization from the account owner.

## 7. Message Deduplication and Audit Trail
- Always respect FR-8 idempotency: mark message IDs as seen only upon successful acceptance, and drop outdated lease versions.
- The decision log (`data/decisions.jsonl` or DynamoDB `decision_log`) is append-only. Never mutate or delete historic decision records.
