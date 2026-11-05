# Edge ↔ Cloud protocol (as implemented and verified)

Verified on 2026-11-05 by running the stack behind a logging proxy (SC-1 delivery + a closed corridor) and
validating every captured message against `contracts/*.schema.json`. Schemas: `contracts/`; topics: `contracts/topics.md`.

## Transport
| | What actually happens |
|---|---|
| **HTTP (default, what the edge uses)** | The **edge always talks HTTP** (`urllib`): it *pulls* cloud→edge messages with `GET /outbox?active=1` every 2 s and *pushes* edge→cloud with `POST`. |
| **MQTT (`NIMBUS_TRANSPORT=mqtt`)** | Cloud-side switch only: the cloud additionally publishes on `nimbus/{cluster}/{lease,renewal,probe}` and subscribes to `nimbus/+/{escalation,checkin}` (in-process stand-in broker or Mosquitto). **The edge has no MQTT client**, so with MQTT on it still works through HTTP (the outbox file `data/outbox.jsonl` is always written too). A real IoT Core edge client is not implemented. |
| Auth | Optional `Authorization: Bearer $NIMBUS_EDGE_TOKEN` on every edge route (NFR-5). |
| Dedup / ordering | Every message has a `message_id` (repeat → `200 {"duplicate": true}`, no effect, FR-8) and carries a lease version (older than current → `409 {"error":"outdated lease_version","current":N}`). |

## Edge → Cloud (HTTP POST)
| Message | Route | Sent when | Body | Reply |
|---|---|---|---|---|
| **Heartbeat** | `POST /fleet/heartbeat` | every 15 s while idle, and at once after a cloud probe; also carries held leases after an outage (FR-12) | `{cluster_id, robots:[{id,state(idle/busy/failed/charging),zone,battery}], stock?, leases?}` | `201 {accepted, health}` (+`reconcile` after an outage). `404` for an unknown cluster |
| **Check-in** (lease renewal) | `POST /checkins` | on accepting a lease, then **every 5 s from a background thread while the lease runs** | `{cluster_id, lease_id, lease_version, timestamp, message_id, robots:[…]}` | `201 {duplicate, accepted, renewed_until?}`. `renewed_until` appears only when the lease is near expiry; the lease **version stays the same, only `expiry_ts` moves**. `409` = stale version → edge drops the lease |
| **CLARIFY** | `POST /escalations` | instruction too ambiguous for the edge | `{type:"CLARIFY", message_id, lease_version, task_id, payload:{question, possible_readings[]}}` | `201 {accepted, status:"needs_clarification"}`; cloud asks the user, then re-leases |
| **UNEXPECTED_EVENT** | `POST /escalations` | something the lease does not allow the edge to solve (blocked corridor with no detour, robot failed, deadline at risk) | `payload:{what_happened, where, robots_saw[], segments?:[{segment_id,state(blocked/closed/crowded)}], robot_zone?}` | `201 {accepted, status, replanned, lease_version}`. Cloud marks the zone closed, sends a `MAP` notice, tries a re-plan (new lease **v+1**), else alerts the operator |
| **OUTPUT_READY** | `POST /escalations` | task finished | `payload:{result_ref}` (e.g. `storage://results/task-000001/delivery-proof.jpg`; survey tasks: `survey.json`) | `201 {accepted, status:"completed"}`; cloud closes the lease, summarises for the user |
| C2 relay decision (C2 mode only) | `POST /relay/decide` | before every non-reflex step when the edge runs as a thin relay | `{cluster_id, lease_id, lease_version, step_index, message_id, cmd?, zone?, target?, …}` | `{action: proceed/wait/reroute/abort, via_zones, source}`; `409` unless the cloud runs C2 |

Escalations that cannot be sent (network down) are kept in the edge's SQLite outbox and re-sent later (FR-10, store-and-forward); results are never lost.
Escalation bodies are validated against `escalation.schema.json` (`400` with the violated paths if not).

## Cloud → Edge (read by the edge from `GET /outbox?active=1`; topic `nimbus/{cluster}/…`)
| Topic | Payload | Edge behaviour |
|---|---|---|
| `lease` | full lease (`lease.schema.json`): `lease_id, version, priority, deadline_ts, expiry_ts, allowed_zones, forbidden_zones, speed_limit, assigned_robots, task_sequence[{cmd(go_to/wait/stop/photo/beep/report), robot, params}], cluster_id, when_to_escalate` | validated, dedup'd by `lease_id-vN`, executed; a higher version replaces the old one. A **stop lease** (single `stop` step) implements operator pause/cancel |
| `renewal` | `{lease_id, version, expiry_ts, message_id}` | extends the running lease's expiry (same version). Renewal is also delivered as `renewed_until` in the check-in reply |
| `probe` | `{type:"PROBE", cluster_id, message_id, ts}` | edge answers with an immediate heartbeat *(added in this pass; before it was ignored and the cluster just waited for its next 15 s heartbeat)* |
| `notice` | `{type, cluster_id?, message_id, ts, …}`; types seen: `MAP` (`text`: zone closed/reopened), `BUNDLE` (`task_id, lease_id, lease_version, bundle:{map nodes+caps+positions, rules…}` — the data/rules the edge needs for the lease so it never fetches mid-task) | logged once; BUNDLE/MAP are informational on the edge today |

`GET /outbox?active=1` hides lease messages of already-finished tasks (stop leases always kept) so a restarted edge does not replay history; plain `GET /outbox` returns everything.

## Other cloud routes (not edge ↔ cloud)
`POST /requests`, `POST /requests/{id}/answer` (app), `POST /tasks/{id}/control|replan` (operator), `POST /fleet/stock`, `GET /fleet|/tasks|/map.json|/metrics|/cost`, `POST /llm/outage` (fault demo).

## Verification result
* Captured live: 16 check-ins, 8 heartbeats, 2 escalations (OUTPUT_READY, UNEXPECTED_EVENT), 2 leases, 3 distinct notices. **Every escalation and lease validated against its schema with zero errors.**
* Probed by hand: CLARIFY → 201 `needs_clarification`; repeated `message_id` → 200 duplicate; `lease_version` 0 → 400 schema error; UNEXPECTED_EVENT missing `where`/`robots_saw` → 400 with paths; unknown cluster heartbeat → 404; stale check-in → rejected.
* Stale-version 409 and re-plan v2 paths are covered by `tests/test_cloud_api.py`.

## Gaps found (not bugs that stop the demo)
1. **No JSON schema files for heartbeat, check-in body, renewal, probe, notice, relay** — only lease, escalation, robot_subtask and the decision-log row (a check-in is validated as a decision-log entry) are published. The shapes above are the de-facto contract.
2. **`UNEXPECTED_EVENT.payload` is open**: `segments` and `robot_zone` are extra fields the schema allows but does not define; the cloud relies on `segments` or a zone name in `where` to know what to close.
3. **Edge has no MQTT client** (see Transport). 
4. `notice` is not listed in `contracts/topics.md`.
5. Check-in renewal only moves expiry when the lease is near it; a lease with a far deadline is never "renewed" because it does not need to be.
