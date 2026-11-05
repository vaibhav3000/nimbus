# Edge ↔ Cloud communication: what is sent, by whom, and what the other side does with it

This document describes what the code does **today** (checked on 2026-11-05 by running the system behind a logging
proxy and by reading `server.py`, `cloud/orchestrator.py`, `edge/supervisor/supervisor.py`, `mqtt_transport.py`).
Where the PRD (docs/PRD_v5.pdf) asks for something, the PRD reference is given; where the code does something the PRD
does not mention, that is said too. Nothing here is assumed: every "does not exist" was searched for in the code.

---

## 1. The short answer

| Question | Answer |
|---|---|
| Is what the **cloud sends** what the **edge expects**? | **Yes** for leases: the cloud's `contracts/lease.schema.json` and the edge's `edge/schemas/lease.schema.json` are identical, and every lease captured live passed it. The other cloud→edge messages (`renewal`, `probe`, `notice`) have **no schema file**; their shape is fixed only by the code (section 6). |
| Is what the **edge sends** what the **cloud expects**? | **Yes** for the three escalations: the two escalation schemas have the same rules (only a cosmetic difference in the edge's copy), and the live `OUTPUT_READY` and `UNEXPECTED_EVENT` passed. `CLARIFY` was sent by hand and accepted. Heartbeat and check-in bodies have **no schema file**; the cloud checks them by hand-written code (section 6). |
| Does the edge speak MQTT? | **No.** The edge only uses HTTP. |
| Does the cloud use MQTT by default? | **No.** By default (`NIMBUS_TRANSPORT` unset) the cloud uses no MQTT at all. |
| Is there authentication? | **Optional, off by default.** See 2.3. |

---

## 2. Transport

### 2.1 Default (no settings): HTTP only
* **Cloud → edge:** the cloud *writes* every outgoing message (lease, renewal, probe, notice) as one line into a file,
  `data/outbox.jsonl`, and serves that file at `GET /outbox`. The edge *pulls*: it calls `GET /outbox?active=1` every 2 seconds
  and picks out the messages whose topic starts with `nimbus/<its cluster>/`. The cloud never pushes to the edge.
* **Edge → cloud:** the edge *pushes* with HTTP `POST` to `/fleet/heartbeat`, `/checkins` and `/escalations`.
* The `topic` strings (`nimbus/east-wing/lease` …) are just labels on the lines in the outbox file here. No broker is involved.

### 2.2 With `NIMBUS_TRANSPORT=mqtt`: HTTP **plus** MQTT on the cloud side
* The cloud keeps doing everything in 2.1 **and additionally** publishes each outgoing message on the same topic over MQTT, and
  subscribes to `nimbus/+/escalation` and `nimbus/+/checkin` so it would also accept those over MQTT.
  So the outbox file is **always** written; MQTT is an extra copy, never a replacement.
* The broker is an in-process stand-in unless a real Mosquitto is configured. (Needs `paho-mqtt` for a real broker.)
* **The edge has no MQTT client** (`grep -i mqtt edge/` finds nothing). So with MQTT switched on, the edge **still** talks HTTP
  exactly as in 2.1, and nothing on the edge side would receive or send an MQTT message. MQTT therefore only matters if some
  other program (not in this repo) subscribes to the broker.
* PRD Fig. 4 shows AWS IoT Core as the cloud–edge channel. The repo does not implement an edge-side IoT/MQTT client; HTTP polling is
  the working stand-in.

### 2.3 Authentication ("bearer token")
* The PRD asks for this in **NFR-5** ("authenticated access; least-privilege IAM; no secrets in code").
* Implementation: if the cloud is started with the environment variable `NIMBUS_EDGE_TOKEN=<secret>`, then the edge-facing routes
  (`/checkins`, `/escalations`, `/fleet/heartbeat`, `/outbox`, `/relay/decide`) require the header
  `Authorization: Bearer <secret>`, otherwise they answer `401`. The edge sends that header if it is started with the same
  `NIMBUS_EDGE_TOKEN` (or `--token`).
* **Default: the variable is not set, so there is no authentication** and `run_all.sh` does not set it. The requester app and
  operator console routes are not token-protected either (only `/fault` and `/llm/outage` need `NIMBUS_DEMO_FAULTS=1` or a token).
* Message-level guards that are always on (independent of the token): every message needs a `message_id` and a lease version.

### 2.4 Rules that apply to every message (PRD FR-8)
* **Repeated `message_id`** → the cloud answers HTTP 200 `{"duplicate": true}` and does nothing.
* **Lease version older than the cloud's current version** → HTTP 409 `{"error": "outdated lease_version", "current": N}`; the
  edge then drops that lease. The edge also drops a lease version it has already run.
* Task id: the edge derives it from the lease id format `lease-<task_id>-v<version>`; the cloud requires that format on check-ins.

---

## 3. Scenarios (what actually travels, in order)

### Scenario A. Normal delivery ("Need an infusion pump at 2B-12, urgent") — PRD SC-1
1. Nurse types the request in the app → `POST /requests` (app → cloud, not edge-related).
2. The cloud finds 2B is paediatric and asks the nurse a question; nurse answers → cloud builds a plan, the rule checker approves it.
3. **Cloud → edge:** a **lease** (version 1) for the east wing is written to the outbox, plus a `notice` of type `BUNDLE` (the map,
   speed caps and rules for that lease, so the edge never has to fetch anything mid-task).
4. The edge picks both up on its next poll (≤ 2 s). It validates the lease (schema, version not seen, zones known), stores it, and
   starts the robots.
5. **Edge → cloud:** a **check-in** immediately, and then one every 5 seconds for as long as the lease runs (section 4 says what the
   cloud does with each).
6. The edge gives the robots one instruction at a time (go_to, wait, go_to, beep, photo, report); these go to the robot, not to the
   cloud, and now appear in the edge log as `EDGE -> R2: …` and `R2 -> EDGE: …`.
7. When the last step is done the edge sends **`OUTPUT_READY`** (`result_ref` = where the delivery photo is stored).
8. The cloud marks the task completed, frees the robots and zones, writes a summary for the nurse. Idle edges keep sending
   heartbeats every 15 s.

### Scenario B. Corridor blocked, no detour inside the lease — PRD SC-2 (FR-4, FR-9, FR-16)
1. A robot is stopped by a closed zone. The edge model tries to handle it **inside** the lease: wait (obstacle may clear) or a detour
   through allowed zones only.
2. If none works, the edge sends **`UNEXPECTED_EVENT`**: what happened, where, what the robots saw, and (optionally) `segments`
   naming the blocked zone.
3. The cloud records it, marks that zone closed in its shared map, sends a `MAP` notice to the affected cluster, and asks the planner
   for a new route. 
   * If a route exists: a **new lease with version +1** is sent (the old version is now outdated and is ignored).
   * If not: it logs `replan_failed` / `reassign_failed` and raises an **operator alert**; the task stays `leased` until someone acts.
4. Seen live: the zone `east_mid_hall` closed → cloud replied `replanned: false`, operator alert (no other route in the east wing).

### Scenario C. Edge needs a clarification: CLARIFY round trip (PRD FR-9, Table 2, FR-4)
PRD: the edge sends CLARIFY "when the instruction or situation is too ambiguous or complex for the edge model"; the cloud may
"answer from memory; ask the user or the room owner".
**When the edge sends it (three situations, all implemented):**
1. A lease step cannot be resolved: a `go_to` whose destination is not on the edge's map and not in the lease's allowed zones
   (candidates = closest allowed zones, so the question is "which place was meant?").
2. The edge model's tactical decision is `ESCALATE_CLARIFY`: an obstacle the robot reports as unclear (words such as
   "unclear", "unidentified", "which", "confirm"). The question is "wait for it to clear, or is the route closed?".
3. A lease step explicitly marked ambiguous (`params.ambiguous = true` with `question` / `possible_readings`) or `cmd: "clarify"`.
   The current cloud never produces such steps; this is for completeness.
**What happens, step by step:**
1. Edge → cloud: `CLARIFY {question, possible_readings}`. The edge **stops the lease's robots and waits**; check-ins continue.
2. Cloud: records it, sets the task to `needs_clarification` and shows the question to the requester (or the model answers from
   memory, in LLM mode).
3. Requester answers in the app → the cloud logs it and sends a notice `{type: "ANSWER", text, task_id, lease_version, message_id}`
   to the cluster in the outbox (same lease version; the task goes back to `leased`).
4. The watcher on the edge reads the ANSWER within 2 s, matches the text against `possible_readings` (or an allowed zone name) and
   **resumes**: situation 1 continues the step with the chosen zone; situation 2 waits and retries (answer "wait") or sends
   UNEXPECTED_EVENT (answer "closed"); situation 3 likewise. An answer that names no usable place → UNEXPECTED_EVENT.
5. While waiting: a newer lease version from the cloud supersedes the wait; if the lease expires first, the robots park safely
   (EXPIRED_PARKED). Optional cap: `NIMBUS_CLARIFY_TIMEOUT` seconds.
6. Answers that were already in the outbox before the question was asked are ignored (they belong to older questions).
Verified live on the stand-in: a shift-clicked "unclear obstacle" in the viewer made the edge send CLARIFY; the cloud showed
`needs_clarification`; my answer "wait for it to clear" reached the edge ~3 s later; R2 waited, retried and the task completed.

### Scenario D. What the cloud does with each check-in (lease renewal)
For every `POST /checkins` the cloud, in this order:
1. Rejects it if `message_id` is missing (400) or was already seen (200 duplicate, nothing else happens).
2. Rejects it if `cluster_id`, `lease_id`, `lease_version` or `timestamp` is missing (400), or `lease_id` is not `lease-<task>-v<n>` (400).
3. Rejects it with 404 if the cluster is unknown, with 409 if the lease belongs to a different cluster.
4. Records one decision-log row ("edge: checkin", PRD FR-20) and marks the cluster **alive** (updates the robot list it sent).
5. If `lease_version` is older than the current one → 409 `outdated lease_version` (the edge stops the old lease).
6. Otherwise tries to **renew**:
   * If the task is already more than **10 minutes past its deadline** → **no renewal** (the cloud must re-decide, not rubber-stamp).
   * New expiry = now + 3 minutes, but never later than deadline + 10 minutes.
   * If that is not later than the current expiry → nothing changes (reply has no `renewed_until`).
   * Otherwise the lease's **expiry moves forward, the version stays the same**; the cloud writes a `renewal` message to the
     outbox (`{lease_id, version, expiry_ts, message_id}`) and replies `201 {accepted, renewed_until}`.
7. The edge reads `renewed_until` from the reply (or the `renewal` message) and moves its own expiry forward.
* **Consequence you should know:** a lease is issued with `expiry = deadline + 180 s`. A renewal is only *needed* once
  now + 3 min passes that, i.e. **after the deadline**. Before that, check-ins reply without `renewed_until`. A task can never run
  longer than **deadline + 10 minutes**: after that the edge parks its robots (PRD: "after expiry, park robots in safe spots").
  With a very slow simulator this cap can end a task even though the robot is still driving.
* The edge treats a lease as expired when its own clock passes `expiry_ts`; when it expires mid-mission the robots go to a safe
  parking zone (PRD FR-5/FR-6) and the cloud is told.

### Scenario D2. While a lease is running: what the edge keeps watching
A background watcher runs for the whole lease (the main thread is busy driving):
* **Check-in every 5 s**: a `409 outdated lease_version` means the cloud has a newer version → the edge stops its robots and drops the lease.
* **Outbox read every 2 s** (`GET /outbox?active=1`, same as when idle):
  * a **higher version of the same task** (re-plan, operator pause/cancel = stop lease, hand-over to another cluster) → robots are
    stopped immediately (`!! LEASE SUPERSEDED ... stopping robots`), the old lease is marked `SUPERSEDED`, and the new lease is run on
    the main loop's next pass;
  * a **renewal** of this lease → expiry moves forward;
  * an **ANSWER** notice for this task → used by a waiting CLARIFY (Scenario C).
* Why both 409 and outbox: they are two views of the same event and each covers the other's blind spot. The 409 is the cloud's
  authoritative "your version is dead" (works even if the outbox read failed once) but carries no content; the outbox carries the
  new lease itself. Either one stops the robots; the outbox is what supplies the replacement.
* Not read during a lease: probes (the 5 s check-in already proves liveness) and MAP/BUNDLE notices (informational).
* Verified live: operator cancel at 20:05:11 → edge stopped R2 at 20:05:12 and then ran the stop lease (v2).

### Scenario E. Network cut and reconnection — PRD FR-6, FR-10, FR-12
1. While cut off, the edge keeps working on the current lease. Escalations it wants to send are stored in its local SQLite outbox
   (store-and-forward, FR-10); results are never dropped.
2. When the link returns, the edge re-sends the stored escalations and sends a **heartbeat that includes the leases it still
   holds** (`leases[]`).
3. The cloud **reconciles** each held lease (FR-12): `ok` (still current), `replaced` (the newer version is re-sent), or `stop` (task
   finished/cancelled/on another cluster: a stop lease with a higher version is sent so no robot is under two leases).

### Scenario F. Operator pause/cancel — PRD FR-22
The cloud sends a **stop lease**: a normal lease with a higher version whose only step is `stop`. The edge treats it like any new
version: it replaces the running lease and stops the robots.

### Scenario G. Cloud checks the edge is alive ("probe")
When a cluster that was alive goes quiet, the cloud sends a `probe`. The edge now answers with an immediate heartbeat (before this
pass it ignored probes and answered at its next regular heartbeat).

### Scenario H. Duplicates, stale versions, bad bodies (tested by hand)
| Sent | Cloud reply |
|---|---|
| same `message_id` again | 200 `{"duplicate": true}` |
| escalation with `lease_version` 0 or missing `where` / `robots_saw` | 400 with the violated field paths |
| check-in or escalation with a version older than the current lease | 409 `{"error":"outdated lease_version","current":N}` (covered by `tests/test_cloud_api.py`) |
| heartbeat from an unknown cluster | 404 |
| escalation for a task that does not exist | 404 |

---

## 4. Message reference

### 4.1 Edge → cloud
| Message | Route | Sent when | Fields |
|---|---|---|---|
| Heartbeat | `POST /fleet/heartbeat` | every 15 s while idle; immediately after a probe; after an outage (with `leases[]`) | `cluster_id`, `robots[{id, state: idle/busy/failed/charging, zone, battery}]`, optional `stock`, optional `leases[]` → reply `201 {accepted, health}` (+`reconcile`) |
| Check-in | `POST /checkins` | on accepting a lease, then every 5 s while it runs | `cluster_id`, `lease_id`, `lease_version`, `timestamp`, `message_id`, `robots[]` → `201 {duplicate, accepted, renewed_until?}` |
| CLARIFY | `POST /escalations` | see Scenario C | `type`, `message_id`, `lease_version`, `task_id`, `payload{question, possible_readings[]}` |
| UNEXPECTED_EVENT | `POST /escalations` | Scenario B | …, `payload{what_happened, where, robots_saw[]}` + optional `segments[{segment_id, state: blocked/closed/crowded}]`, `robot_zone` |
| OUTPUT_READY | `POST /escalations` | task finished | …, `payload{result_ref}` (delivery: `…/delivery-proof.jpg`; survey: `…/survey.json`) |
| Relay decision | `POST /relay/decide` | only in configuration C2 (cloud-only), before every step | `cluster_id, lease_id, lease_version, step_index, message_id, …` → `{action: proceed/wait/reroute/abort, via_zones, source}`; 409 unless the cloud runs C2 |

### 4.2 Cloud → edge (the edge reads these from `GET /outbox?active=1`)
| Topic | Contents | What the edge does |
|---|---|---|
| `nimbus/<cluster>/lease` | the lease: `lease_id, version, priority (routine/urgent), deadline_ts, expiry_ts, allowed_zones, forbidden_zones, speed_limit, assigned_robots, task_sequence[{cmd: go_to/wait/stop/photo/beep/report, robot, params}], cluster_id, when_to_escalate` | validates, runs it; a higher version replaces the old one. **Note:** the edge does not use `when_to_escalate`; it decides when to escalate from what the robots report. |
| `nimbus/<cluster>/renewal` | `lease_id, version, expiry_ts, message_id` | extends the running lease's expiry |
| `nimbus/<cluster>/probe` | `type: "PROBE", cluster_id, message_id, ts` | when idle: answers with a heartbeat. During a lease: not read (the check-in does the same job) |
| `nimbus/<cluster>/notice` | `type`, `message_id`, `ts`, plus: `MAP` → `text, cluster_id` (a zone closed/reopened); `BUNDLE` → `task_id, lease_id, lease_version, bundle{map nodes with speed caps and positions, rules}`; `ANSWER` → `text, task_id, lease_version, message_id` (the reply to a CLARIFY) | prints MAP/BUNDLE once and does not act on their contents today; **acts on ANSWER** (resumes a waiting CLARIFY) |

`GET /outbox?active=1` omits lease messages of tasks that are already completed or rejected (stop leases are always kept), so a
restarted edge does not repeat old work. Plain `GET /outbox` returns the whole history.

---

## 5. Does each side's output match the other's expectation? (verdict per message)
| Message | Producer's output | Consumer's expectation | Verdict |
|---|---|---|---|
| lease | cloud builds it, validates against `lease.schema.json` | edge validates against `edge/schemas/lease.schema.json` (identical file content) | **Match** (validated live) |
| escalations ×3 | edge builds them | cloud validates against `escalation.schema.json` | **Match** (OUTPUT_READY, UNEXPECTED_EVENT live; CLARIFY by hand) |
| check-in | edge body above | cloud checks required fields by hand code and a decision-log schema on its own row | **Match** (16 live), but no published schema for the body |
| heartbeat | edge body above | cloud reads `cluster_id`, `robots`, `stock`, `leases` | **Match** (8 live), no published schema |
| renewal | cloud | edge reads `lease_id`, `expiry_ts` | **Match by code**, no schema; not seen live because renewal only triggers after a deadline passes |
| probe | cloud | edge reads `message_id` | **Match by code**; not seen live |
| notice (MAP, BUNDLE) | cloud | edge only prints the type | **Match** (nothing to disagree on) |

## 6. Gaps (nothing below blocks the demo)
1. **No schema files** for heartbeat, check-in, renewal, probe, notice, relay; they are defined only by code.
2. `UNEXPECTED_EVENT.payload` is open: `segments` and `robot_zone` are extras the schema neither defines nor forbids; the cloud
   relies on `segments` (or a known zone id in `where`) to know what to close.
3. The edge has no MQTT/IoT client (HTTP polling stands in for IoT Core).
4. `notice` is not listed in `contracts/topics.md`.
5. Authentication is off unless `NIMBUS_EDGE_TOKEN` is set.
6. The edge ignores `when_to_escalate`.
7. `CLARIFY` from the edge is now triggered by an unresolvable destination or an unclear obstacle (Scenario C), but the cloud itself still never issues an ambiguous step, so in normal cloud-issued leases it appears only for unclear obstacles. The answer-matching is simple (reading text / zone name), not a language-model judgement.
8. A task cannot run past deadline + 10 min (see Scenario D); relevant when the simulator is much slower than real time.
