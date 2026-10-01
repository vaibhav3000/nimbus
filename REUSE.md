# REUSE.md — what Person 4 reused, and from where

The mediator's rule: do not rewrite what already matches the PRD. Scan result:
the old `Projects/NIMBUS_WORKSPACE` targets PRD **v3.1**, whose lease model
(corridor/waypoints/epoch/seq/hold_points, L0–L3 rung ladder) is a different
domain from PRD v5.0's lease (zones/deadline/expiry/version per the scope).
No v5.0 implementation existed anywhere in the workspace, so the following was
reused as *material*, not as working v5.0 code:

| Reused | From | How |
|---|---|---|
| JSON Schema house style: draft 2020-12, `https://nimbus.ds252.local/schemas/…` `$id`s, prose descriptions citing FRs, `additionalProperties: false` | `NIMBUS_WORKSPACE/contracts/*.schema.json` | All four v5.0 contracts follow the same conventions |
| Cross-field checks outside JSON Schema, strict error lists from one `validate` entry point | `NIMBUS_WORKSPACE/nimbus_core/contract_validation.py` | `contracts.py` mirrors the pattern (`semantic_lease_errors` + schema errors; here `deadline_ts <= expiry_ts` replaces `renew_at_ms < ttl_ms`) |
| Dependency set: `jsonschema>=4.21`, `pytest>=8.0`, pyproject `[tool.pytest.ini_options] testpaths=["tests"]` | `NIMBUS_WORKSPACE/requirements-dev.txt`, `pyproject.toml` | Both files adapted; no new dependency added |
| Registry-based `$ref` between schema files (referencing lib ships with jsonschema) | `NIMBUS_WORKSPACE` schema loading approach | `lease.schema.json` refs `robot_subtask.schema.json` |
| Stdlib `http.server` + `ThreadingTCPServer` serving JSON on Windows | `Downloads/Cloud Computing/app/app.py` (DS252 week-4 lab service) | `server.py` uses the same stdlib pattern (ThreadingHTTPServer), zero-framework |
| PRD-derived vocabulary and decision rows | `NIMBUS_WORKSPACE/Context/` (decision-log/traceability conventions) | FR-20 row shape (who/inputs/output/lease_version) and the "every decision recorded" discipline |
| AWS single-region, Terraform, ECS-Fargate-not-Lambda rationale | PRD v5.0 Fig 4 + Sec. 6 design decisions (and old workspace's `infra/terraform/` skeleton shape: flat main.tf + variables.tf) | `infra/terraform/` resource set follows PRD Fig 4's component list |
| Token accounting awareness (150-token decision cap, per-call LLM cost) | PRD Sec. 2 + Sec. 7 | `cost.py` counters and the 150-token stand-in constant |

Not reused (and why): old lease/event/action schemas (v3.1 domain), lease state
machine / ordering / tactical SLM modules (out of scope, Person 2/3), Gazebo
and hardware readiness code (out of scope), the week-5 Lambda snippets
(PRD Fig 4 explicitly rejects Lambda).

Nothing else in the workspace (DS252 lecture notes, week-2/4/6 labs) contained
Nimbus-relevant code.
