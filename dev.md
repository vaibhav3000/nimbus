# Nimbus developer guide

This guide is for someone who will read, change or extend the code. It assumes you have read the
[README](README.md), which covers what Nimbus is for and how to run it. Terms such as lease, edge, escalation and
domain pack are defined there.

Contents:

1. [The big picture in code](#1-the-big-picture-in-code)
2. [Cloud: `server.py` and `cloud/`](#2-cloud-serverpy-and-cloud)
3. [Domain packs: `packs/`](#3-domain-packs-packs)
4. [Contracts: `contracts/`](#4-contracts-contracts)
5. [Edge: `edge/`](#5-edge-edge)
6. [Robots and simulation: `simulation/`](#6-robots-and-simulation-simulation)
7. [Web apps: `static/`](#7-web-apps-static)
8. [AWS deployment: `infra/terraform/`](#8-aws-deployment-infraterraform)
9. [Configuration reference](#9-configuration-reference)
10. [Testing](#10-testing)
11. [Experiments](#11-experiments)
12. [How to extend](#12-how-to-extend)
13. [Known issues](#known-issues)

---

## 1. The big picture in code

Three processes talk over HTTP and exchange JSON documents whose shapes are fixed by `contracts/`:

```
 requester app / operator console (browser, static/)
        |  HTTP
        v
 server.py  -- cloud: HTTP API, task store, decision log, cost counters
   cloud/   -- decisions: understand, schedule, allocate, check, lease, react, remember
        ^  leases (GET /outbox)            |  escalations, check-ins, heartbeats (POST)
        |                                  v
 edge/run_supervisor.py  (one per cluster: east-wing, west-wing)
   edge/supervisor/      -- runs leases, geofence, parking, store-and-forward, takeover
   edge/slm/engine.py    -- small local model (Ollama / llama.cpp) + rule fallback
   edge/store/           -- SQLite: leases, commands, decision log, outbox
        |  Python calls (execute_command: go_to, wait, stop, photo, beep, report)
        v
 edge/mock/sim_robot.py  -- driver: Gazebo (ROS 2) or kinematic stand-in
 simulation/hospital_world/ground_robot_sdk/  -- navigation, A*, avoidance, safety stop
```

| Step | Who | Code |
| --- | --- | --- |
| Request arrives | cloud | `server.py` `_post_request` |
| Understand (model, then grounding against the words) | cloud | `cloud/orchestrator.py` `handle_request`, `_llm_intent`, `_ground`; `cloud/nlp.py` |
| Ask a question / resume from the answer | cloud | `_clarify_outcome`; `server.py` `_post_answer` |
| Rank candidates, model chooses, checker verifies | cloud | `cloud/allocator.py`, `orchestrator._candidates`, `cloud/checker.py` |
| Build and commit the lease | cloud | `cloud/leasebuilder.py`, `cloud/leases.py` |
| Publish to the edge | cloud | outbox (`server.py` `publish`), optional MQTT (`mqtt_transport.py`) |
| Run the lease | edge | `edge/supervisor/supervisor.py` `process_lease` |
| Local decision on an event | edge | `edge/slm/engine.py` `evaluate_decision` |
| Escalate | edge | `send_escalation_to_cloud` (stored in SQLite first) |
| React | cloud | `server.py` `process_escalation`, `cloud/react.py` |
| Finish | cloud | `orchestrator.finish`, `complete_summary`, `cloud/memory.py` |

The table that lists every message, its fields and who sends it is in
[docs/EDGE_CLOUD_PROTOCOL.md](docs/EDGE_CLOUD_PROTOCOL.md).

---

## 2. Cloud: `server.py` and `cloud/`

`server.py` is the HTTP boundary. It uses only the Python standard library, so the cloud runs anywhere with
`python server.py`. It holds the task store (`store.py`), the decision log, cost counters (`cost.py`), and the
publish function. Every decision is delegated to an `Orchestrator` (`cloud/orchestrator.py`).

### HTTP API

| Method and path | Who calls it | What it does |
| --- | --- | --- |
| `GET /`, `/operator`, `/map` | browser | requester app, operator console, map page |
| `POST /requests` | requester app | new request; replies with the task id and status (`leased`, `queued`, `needs_clarification`, `rejected`) |
| `POST /requests/{id}/answer` | requester app | answer to the cloud's question |
| `GET /tasks`, `/tasks/{id}` | apps | task state |
| `GET /tasks/{id}/timeline` | apps, experiments | decision-log rows for the task (FR-20) |
| `GET /tasks/{id}/trace` | developers | every message and model call for the task, with prompts, tools, tokens and latency |
| `POST /tasks/{id}/control` | operator console | `{"action": "pause" or "cancel"}`; publishes a stop lease (FR-22) |
| `POST /tasks/{id}/replan` | operator | ask for a new plan |
| `GET /outbox?active=1` | edge | the current cloud-to-edge messages (leases, renewals, notices, probes) |
| `POST /fleet/heartbeat` | edge | cluster alive, robot states and zones |
| `POST /checkins` | edge | lease check-in; renews or answers 409 for an outdated version |
| `POST /escalations` | edge | `CLARIFY`, `UNEXPECTED_EVENT`, `OUTPUT_READY` |
| `POST /relay/decide` | edge in C2 only | one model call per step (cloud-only baseline) |
| `GET /fleet`, `/inventory`, `/map.json` | apps | clusters, robots, stock, map, closures, restrictions, live leases |
| `POST /fleet/stock` | operator | set stock counts |
| `POST /map/reopen` | operator | reopen a closed zone |
| `POST /reset-memory` | operator | clear habits, closures and restrictions (do not use on a shared deployment) |
| `GET /llm-status`, `/metrics`, `/cost`, `/trace`, `/notifications` | apps, monitoring | model status, latency percentiles, cost counters, notices |
| `POST /llm/outage`, `GET/POST /fault` | experiments | fault injection; protected by `NIMBUS_DEMO_FAULTS=1` or an admin token |

Edge endpoints require `Authorization: Bearer <NIMBUS_EDGE_TOKEN>` when that variable is set (NFR-5).

### `cloud/` modules

| Module | Responsibility |
| --- | --- |
| `orchestrator.py` | The pipeline: context, intent, grounding, allocation, check, lease; re-plan, hand-over, decide_step (C2), finish, cancel, drain |
| `llm.py` | Model access behind one interface: `BedrockLLM` (Converse API, forced tool use, temperature 0), `RuleBasedLLM` (offline stand-in), `CircuitBreakerLLM`, `CassetteLLM` (record once, replay for free) |
| `agent.py` | A bounded agent loop: the model may call read-only look-up tools, then must end with one decision tool |
| `tools.py` | Tool registry and JSON schemas for every model step (intent, allocation, react, step, result) |
| `readtools.py` | The look-ups the model may call (fleet, stock, route, policies, task log, map) |
| `context.py` | What the model is allowed to see: fleet, stock, restrictions, rules, memory, task log |
| `nlp.py` | Deterministic request parsing from the pack's patterns; used by C3, by the fallback and to ground model answers |
| `allocator.py` | Ranks eligible clusters and robots (nearest free robot), explains exclusions |
| `checker.py` | The Rule Checker: re-derives every fact and returns every violation (veto, hold, clarify) |
| `rules.py` | A small safe condition language for pack rules (no `eval`) |
| `leasebuilder.py` | Builds the lease document from a checked allocation and the pack's task template |
| `leases.py` | Lease storage with atomic conditional commits: one robot cannot be in two live leases |
| `aws_dynamo.py`, `aws_store.py` | DynamoDB versions of the lease, stock and task stores |
| `scheduler.py` | Priority then earliest deadline then arrival order; a blocked entry never holds up runnable ones |
| `fleet.py` | Clusters, robots, health (unknown, healthy, stale, down by check-in age), stock |
| `topology.py` | The map as a graph: zones, aliases, areas, routes |
| `mapstate.py` | Shared map of closures reported by edges, with expiry |
| `policy.py`, `statements.py` | Runtime restrictions such as isolation, and statements that declare or lift them |
| `react.py` | Reaction to edge reports: update the map, choose re-plan, hand-over, wait, operator or no action |
| `legs.py` | Hand-over between clusters as legs of one task |
| `survey.py` | Survey tasks (T7): robots go and look, reports are merged |
| `memory.py` | Habits per place, used to skip repeat questions (FR-19) |
| `notify.py` | Notices to requester, operator and affected clusters only (FR-11) |
| `ticker.py` | Time-driven work: expiry, deadlines, silent clusters, queue retries |
| `faults.py` | Model fault injection (`POST /llm/outage`, `NIMBUS_LLM_OUTAGE`) |
| `trace.py`, `metrics.py` | Per-task trace of every message and model call; counters and percentiles |
| `results.py`, `bundle.py` | Result storage (S3 or local); the data bundle sent with each lease |
| `sqs_consumer.py` | Optional ingest of edge messages from SQS (IoT Core rule) |

### Safety properties that the code guarantees

These hold whatever the model answers:

- **No lease without the checker.** `orchestrator._allocate` builds a lease only after `checker.check` returns
  `ok`, and validates it against `contracts/lease.schema.json`.
- **Bounded model retries.** The model proposes at most twice per request. A malformed answer, or two vetoed
  proposals, hand the decision to the classical allocator.
- **Grounded understanding.** `handle_request` and `_ground` check the model's task against the requester's words.
  A destination nobody named becomes a question. Priority follows the pack's urgent wording. An unstated deadline
  takes the priority's default.
- **Version fencing.** A check-in or escalation with an older lease version gets HTTP 409, and the edge stops.
- **Model outages.** `CircuitBreakerLLM` opens after 3 consecutive failures; every model step then has a rule-based
  path, and replies carry a reduced-autonomy notice.

---

## 3. Domain packs: `packs/`

A pack holds everything domain-specific. The core code never names a ward, an item or a rule; a test
(`tests/test_packs.py`) enforces that.

| File | Contents |
| --- | --- |
| `pack.json` | items and variants, priorities (patterns, default deadlines), rules (e.g. SOP-PAED-01), restriction kinds, task templates, request patterns, questions |
| `topology.json` | nodes (kind, area, aliases, flags such as paediatric) and edges with lengths |
| `fleet.json` | clusters, their area, home store, robots, starting stock |
| `policies.json` | rule documents retrieved into the model's context |
| `prompts/*.txt` | system prompts for each model step: intent, allocation, react, step, result |
| `scenarios.json` | named scenarios used by tests and demos |

| Pack | Use |
| --- | --- |
| `hospital_sim6` | The default. The simulated hospital map (generated from `simulation/hospital_world/topology/` plus `overlay.json`), two clusters, six robots. |
| `hospital_sim` | Two-robot version of the same map, used by many tests |
| `hospital` | The abstract hospital of the original scenarios (wards 2A to 3A, gates W0 to E4) |
| `rescue` | A disaster-response pack, showing the core is domain-neutral |
| `scale10` | 10+ robots over 3 clusters for scale tests (`scripts/make_scale_pack.py`) |

Choose a pack with `NIMBUS_PACK=<name>`.

---

## 4. Contracts: `contracts/`

JSON Schemas that both cloud and edge validate against:

- `lease.schema.json`
- `escalation.schema.json`
- `decision_log_entry.schema.json`
- `robot_subtask.schema.json`
- `topics.md` (MQTT topic names)

`contracts.py` loads them and adds cross-field rules. For example, the robots of a lease must be in its cluster.
Change a schema only together with both sides and their tests.

---

## 5. Edge: `edge/`

Start one supervisor per cluster with `python edge/run_supervisor.py --cluster east-wing ...`. Its main options:

| Option | Meaning |
| --- | --- |
| `--cloud-url` | where the cloud is (default from `NIMBUS_CLOUD_URL`) |
| `--robot-backend mock or sim` | in-memory robots, or the simulation driver |
| `--sim-mode auto, gazebo or kinematic` | for `sim`: real Gazebo through ROS 2, or the kinematic stand-in on the same map |
| `--system-mode C1, C2 or C3` | edge model, cloud-only relay, or rules only |
| `--slm-mode auto, ollama, llama_cpp, mock or rules_only`, `--slm-model` | how the edge model runs |
| `--token` | edge token (NFR-5) |

| Module | Responsibility |
| --- | --- |
| `supervisor/supervisor.py` | Fetches leases, validates them, splits them into robot commands, runs them with a health watchdog, handles events through the edge model, escalates, watches for newer versions while robots move, checks in and renews |
| `supervisor/geofence.py` | Lease boundary checks on every destination and detour (FR-5) |
| `supervisor/parking.py` | Safe parking on expiry or emergency, never in a doorway or lift (FR-6) |
| `supervisor/outbox_worker.py` | Store-and-forward uploader (FR-10) |
| `supervisor/reconnection.py` | Reconnection summary after an outage (FR-12) |
| `supervisor/artifacts.py` | Delivery-proof artefacts with hashes for `OUTPUT_READY` |
| `slm/engine.py` | Prompt, call and validate the small model; deterministic rules as fallback; an expiry-near rule always parks |
| `store/local_store.py` | SQLite: leases, commands, decision log, outbox |
| `mock/mock_robot.py` | Fast in-memory robot for tests |
| `mock/sim_robot.py` | Driver for the simulated robots: name resolution, routing over the topology, frame conversion, sensor checks |

---

## 6. Robots and simulation: `simulation/`

### `hospital_world/`
- **World and robots:** `hospital.sdf` (Gazebo Fortress hospital floor) and `models/robots/mit_racecar` (the robot
  with camera and range sensor).
- **Topology:** `topology/` holds the place graph (`hospital_topology.json`, `topology_waypoints.json`), the PRD
  vocabulary mapping (`scenario_locations.json`) and the resolver.
- **Robot SDK:** `ground_robot_sdk/`
  - `runtime.py`: the entry point
  - `navigation.py`: point-to-point goals, A* tracking, avoidance
  - `planning.py`: occupancy grid and A*
  - `avoidance.py`: dynamic obstacles
  - `safety.py`: reflex stop below everything else
  - `gazebo_adapter.py`: ROS 2 bridge
  - `mock_adapter.py`: tests
- **Scripts:** `scripts/validate_topology.py` checks every corridor against the walls in Gazebo; the launch scripts
  start one, two or six robots.

### `lite/viewer.py`
The Gazebo-less live view. Kinematic robots post their pose to it. Click a node to close it (an obstacle), or
shift-click for an unclear obstacle that triggers `CLARIFY`.

---

## 7. Web apps: `static/`

Plain HTML and JavaScript served by `server.py`:

- `index.html` with `app.js`: requester app
- `operator.html` with `operator.js`: operator console
- `map.html`: live map

They only use the HTTP API above.

---

## 8. AWS deployment: `infra/terraform/`

`main.tf`, `cloud.tf`, `variables.tf` and `outputs.tf` describe the following:

| Component | Used for |
| --- | --- |
| API Gateway | public entry point |
| Container service | runs `server.py` with `NIMBUS_BACKEND=aws` and `NIMBUS_LLM=bedrock` |
| DynamoDB | leases, tasks, robot status, decision log, and the cloud state table |
| S3 | results and logs |
| IAM | least-privilege roles |
| CloudWatch | metrics and alarms |

Setting `NIMBUS_BACKEND=aws` switches the stores to DynamoDB (`cloud/aws_dynamo.py`, `cloud/aws_store.py`).

---

## 9. Configuration reference

The most used environment variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `PORT`, `DATA_DIR` | `8000`, `data` | where the local cloud listens and keeps its files |
| `NIMBUS_PACK` | `hospital_sim6` | domain pack |
| `NIMBUS_MODE` | `C1` | cloud configuration: C1, C2 or C3 |
| `NIMBUS_LLM` | `fake` | `bedrock` for Amazon Bedrock; otherwise the rule-based stand-in |
| `NIMBUS_BEDROCK_MODEL` | (none) | e.g. `apac.amazon.nova-lite-v1:0` |
| `NIMBUS_CASSETTE`, `NIMBUS_CASSETTE_MODE` | (off) | record real model calls to a file, or replay them for free |
| `NIMBUS_BACKEND` | `local` | `aws` for DynamoDB and S3 |
| `NIMBUS_TICK_S` | `5` | how often time-driven work runs |
| `NIMBUS_ALLOW_UNKNOWN` | `1` | lease to clusters that have not checked in yet |
| `NIMBUS_EDGE_TOKEN`, `NIMBUS_ADMIN_TOKEN` | (off) | bearer tokens for edge endpoints and fault injection |
| `NIMBUS_DEMO_FAULTS` | `0` | allow `/llm/outage` and `/fault` without a token (local only) |
| `NIMBUS_LLM_OUTAGE` | (off) | `after_s:duration_s` scheduled model outage |
| `NIMBUS_CLOUD_URL`, `NIMBUS_LOCAL_CLOUD` | deployed | which cloud `run_all.sh` and the edges use |
| `NIMBUS_SLM_MODE`, `NIMBUS_SLM_MODEL` | `auto`, `qwen2.5:1.5b` | edge model |
| `NIMBUS_SIM_VIEWER_URL` | (off) | kinematic robots post poses to the live viewer |
| `NIMBUS_SENSOR_TIMEOUT`, `NIMBUS_GOAL_TIMEOUT` | `1.0`, `600` | robot driver limits (raise on slow hosts) |
| AWS: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, `AWS_REGION` | | needed for Bedrock and DynamoDB |

---

## 10. Testing

```bash
python -m pytest -q
```

This runs both `tests/` (cloud and shared) and `edge/tests/`. The simulation SDK has its own tests:

```bash
cd simulation/hospital_world && python3 -m unittest discover -s tests -v
```

- Cloud tests build an `Orchestrator` with a fixed clock and a scriptable model (`tests/cloud_helpers.py`), so
  fallback, veto and re-plan paths are deterministic. To simulate a misbehaving model, pass an `override`.
- `tests/test_intent_grounding.py` and `tests/test_replan_robot_fault.py` cover the fixes found by the experiments.
- DynamoDB code is tested against `moto` (`requirements-aws.txt`).

---

## 11. Experiments

`experiments/cloud/` holds the cloud experiments C1 to C9; [experiments/cloud/README.md](experiments/cloud/README.md) lists
them with commands.

The robot and edge experiments (R1 to R10) and the network and infrastructure experiments (N1 to N10) are still to
be added. They can reuse `ResultDir` from `experiments/cloud/lib.py` so their folders have the same layout and
`report.py` collects them. The format every results folder must follow is in
[docs/RESULTS_GUIDE.md](docs/RESULTS_GUIDE.md). Their result rows in the README are placeholders until then.

- **Harness:** `experiments/cloud/lib.py`.
  - `start_local` starts `server.py` on a free port with an empty data folder.
  - `open_target` opens a deployed URL instead.
  - `Edges` simulates edge clusters that only act on the experiment's own tasks.
  - `ResultDir` writes the results folder: `raw/`, `summary.csv`, `plots/`, `env.json` and `README.md`.
  - Statistics helpers: Wilson and bootstrap intervals.
- **Rules every experiment follows:**
  - every number comes from that run's raw records
  - every result says where it came from (`real_bedrock`, `mock_llm`, `measured`)
  - every task created on a shared deployment is cancelled at the end
- **Summary table:** `python experiments/cloud/report.py` rebuilds `results/final/RESULTS.md`.
- **Older runners:** `scripts/` holds the earlier E1 to E7 runners, audits (`audit_iam.py`, `audit_data.py`) and
  tools: `trace.py` prints a task's full ladder of messages and model calls; `replay_check.py` reruns from a seed and
  diffs the logs.

---

### Changes from the team review (cloud side)

- A message id is reserved while its request is processed (`Store.reserve_message_id`), so parallel deliveries of one
  id make one task. The reservation is released when the request ends; a failed delivery does not burn its id.
- A repeated `OUTPUT_READY` (new message id, same result) is acknowledged without running completion again.
  Events for a finished task are logged but not re-planned.
- A check-in is logged before the renewal it causes; a stale check-in is rejected before it is logged.
- The scheduler promotes a task whose deadline has passed to the most urgent class (earliest deadline first), so
  routine work cannot wait for ever behind new urgent work.
- Cost counters: Fargate time is metered from the clock in the tick loop (`NIMBUS_FARGATE_GB`, default 1), not as a
  constant per decision; the CloudWatch metric count only grows when metrics are emitted.
- `POST /results/upload-url` and `PUT /results/upload/<task>/<name>` (local) / presigned S3 PUT (cloud) for result files.
- `contracts/notice.schema.json` and the `notice` topic are documented in `contracts/topics.md`.
- SQS: receive with a 120 s visibility timeout, bounded client timeouts; `infra/terraform/cloud.tf` adds a dead-letter
  queue (5 receives) and the `dynamodb:TransactWriteItems` permission the lease transactions need.
- `GET /trace` with a bad number answers 400; request bodies are limited to 1 MB.

## 12. How to extend

| Goal | Where to change |
| --- | --- |
| A new site or domain | Add a pack folder (copy `hospital_sim6`), validate it against `packs/pack.schema.json`, set `NIMBUS_PACK` |
| A new item, priority or rule | `pack.json` (rules use the condition language in `cloud/rules.py`) |
| A new task type | A task template in `pack.json`, and a planner like `cloud/survey.py` if robots must do more than fetch and deliver |
| A new model | Implement `converse(...)` returning `LLMResult` (see `cloud/llm.py`), wire it in `Orchestrator.from_env` |
| A new model tool | `cloud/tools.py` (schema), the prompt in `packs/*/prompts`, and the handling code |
| A new edge decision | the action list and rules in `edge/slm/engine.py`, the handling in `supervisor.process_lease` |
| A different robot | a new `RobotAdapter` in `ground_robot_sdk/` (see `gazebo_adapter.py`), or a new driver beside `edge/mock/sim_robot.py` |

---

## Known issues

- **19 failing tests on this branch.** They came in with the latest merges from `main` (navigation, map and topology changes).
  The robot map dropped the corridor that ran through a wall into ward 3. This was correct for the robots, but ward
  3 (`south_room_approach`) is now unreachable in the robot map and in the `hospital_sim` pack, while
  `hospital_sim6/topology.json` still has the edge. The ward needs a route through its real doorway in
  `simulation/hospital_world/topology/`, after which the packs are regenerated (`scripts/build_sim_topology.py`).
- **No turnaround yet.** The robot SDK refuses any goal behind the robot ("refusing an unsafe forward U-turn"), so
  the return leg of a trip, or a detour that needs the robot to turn back, is reported as `BLOCKED`. A three-point
  turn in `ground_robot_sdk/navigation.py`, checked against the occupancy map, would fix it.
- **C2 relay.** The cloud's `POST /relay/decide` exists and the C9 experiment drives it, but the edge supervisor in
  C2 mode still forwards events as `UNEXPECTED_EVENT` instead of calling it per step.
- **Deployed experiments.** The deployment is shared. Experiments default to a local cloud and never contain the
  deployment's address: to use it, set `NIMBUS_CLOUD_URL` (or pass `--target <url>`). The harness refuses to start
  while real edges are connected. An interrupted C8 run on 6 Nov left queued test tasks;
  `experiments/cloud/cleanup_deployed_c8.py` cancels exactly those.
- **Not yet protected: the requester and operator routes.** Only the edge routes (`EDGE_PATHS`) check a token.
  `POST /requests`, `/tasks/<id>/control`, `/map/reopen`, `/inventory/*` and `/reset-memory` rely on the API Gateway
  in front of the service, and `static/operator.js` calls the last two directly. Closing this needs a role-based
  check in `server.py` plus a token in the web apps (infrastructure and web app work).
- **Task records are shared objects.** `Store.get_task` returns the live dictionary, and the ticker changes tasks
  outside the state lock. Writes go through `put_task` under the lock, so no lost update has been seen, but a
  reader can see a task halfway through an edit. Returning copies needs the callers that mutate-then-`put_task` to
  be changed together.
- **Result files and the edge.** The cloud now hands out upload targets (`POST /results/upload-url`), but the edge
  does not call it yet, so a `storage://` result is still only a pointer.
