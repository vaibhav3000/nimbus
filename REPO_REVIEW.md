# Nimbus — Repository Review & Working Explainer

*A standalone review of the `nimbus_final` repository: what the system does, how the
code is put together, how a request flows through it, what state it is in today, and
where the open questions are. Written so it can be read cold (e.g. by another model)
as a basis for brainstorming. Nothing here needs the codebase open to follow.*

Date of review: 2026-11-07. Branch: `main`.

---

## 1. What Nimbus is (in one paragraph)

Nimbus orchestrates a fleet of delivery robots spread across the wings of a large site
(the running example is a hospital). A person types a plain-language request — *"Need an
infusion pump at 2B-12, urgent"*. A **cloud LLM** (Amazon Bedrock) interprets it, asks a
clarifying question if it is vague, and proposes a plan. A **deterministic rule checker**
in the cloud verifies that plan against hard safety rules. If it passes, the cloud issues
a **lease**: a time-limited, zone-bounded grant of authority to one **edge cluster** (the
on-site computer for a wing). Inside the lease, the edge and its robots act autonomously —
including through multi-minute network outages — using a small on-site model (SLM) and
classical navigation. The edge contacts the cloud again only to clarify, report an
unexpected event, or deliver a result. The central design bet is **"the LLM proposes, a
rule checker disposes, and authority is leased, not streamed."**

The academic framing (DS252, IISc) is a three-tier **cloud / edge / robot** architecture
where reasoning is deliberately split: a heavy global model in the cloud, a small local
model per wing, and model-free navigation + safety-stop on the robot.

---

## 2. The three tiers

```
         ┌─────────────────────────── CLOUD (AWS, "global brain") ──────────────────────────┐
         │  Requester app  +  Operator console  (static web apps)                           │
         │  Orchestrator:  context → LLM intent → LLM allocation → RULE CHECKER → lease      │
         │  Lease Manager (atomic, conditional commits)   Shared map   Memory   Scheduler    │
         │  Bedrock LLM    DynamoDB    SQS    IoT Core    Fargate    S3    CloudWatch         │
         └───────────────▲───────────── lease (down) ──────────┬──────────────────────────┘
                         │  escalations / results / check-ins   │  lease + renewals
                         │  (IoT Core → SQS → cloud)            ▼
         ┌───────────────┴──────────── EDGE cluster per wing (east-wing, west-wing) ─────────┐
         │  Supervisor daemon:  enforce lease bounds, local SQLite, outbox (store-&-forward), │
         │  SLM for tactical decisions, geofence, parking, artifact (camera) packaging        │
         └───────────────▲────────────────────────────────────────────────────────────────┘
                         │  subtasks (go_to, pick, look) + telemetry
                         ▼
         ┌──────────────────────────── ROBOTS (6 Ackermann "racecars" in Gazebo) ───────────┐
         │  Classical navigation (A*/pure-pursuit-style control), model-free safety stop      │
         └──────────────────────────────────────────────────────────────────────────────────┘
```

- **Cloud** = global view + heavy model + the single authority that issues leases. Runs on
  AWS in production; runs as a local Python HTTP server (`server.py`) for development.
- **Edge** = one cluster per wing. A long-running supervisor daemon that *always* drives its
  robots, holds a lease, and survives disconnection. One SQLite DB per wing.
- **Robot** = navigation + a reflexive safety stop. No language model. In this repo the
  robots are simulated (Gazebo Fortress, Ackermann-steered cars), with a kinematic
  fallback simulator for machines without Gazebo/ROS.

---

## 3. The lease protocol (the core idea)

A **lease** is the contract between cloud and edge (JSON schema in
`contracts/lease.schema.json`). It grants a specific **cluster** + **robot(s)** the right to
perform a task within:

- a set of **allowed zones** and explicit **forbidden zones**,
- a **speed limit**,
- a **deadline** and an **expiry** (expiry ≪ deadline; the edge must renew to keep going),
- a **version** number (monotonic per task).

Key invariants, enforced by the **Lease Manager** (`cloud/leases.py`, backed by DynamoDB
conditional writes / `TransactWriteItems` in production, or an in-memory lock for tests):

- **One live holder per robot**, and per *exclusive* zone (e.g. a gate).
- **Version fencing**: a message that quotes an old lease version is rejected with the
  current version (`StaleVersion`), so stale edge traffic can never take effect. This is
  how "no duplicate actions" (FR-8) and safe hand-over are guaranteed.
- **Renewal is LLM-free** (`Orchestrator.renew`): a check-in extends expiry up to a cap
  past the deadline; past that cap the cloud must re-decide rather than rubber-stamp.
- Leases are **immutable, append-only history** (an audit row per issued `lease_id`).

The edge's autonomy is *bounded* by the lease: it may detour, retry, and act locally inside
the allowed zones, but it cannot cross a forbidden zone or act after expiry. This is what
makes offline operation safe — authority was granted ahead of time, not streamed per move.

---

## 4. A request's life cycle (end to end)

1. **Ingress.** Requester app `POST /requests`. Two ingress styles:
   - **Prose** (`{"text": "..."}`) — the normal C1/C2 path.
   - **Structured form** (`{"form": {...}}`) — the C3 baseline path (see §6).
2. **Context pack.** The orchestrator builds a `Context`: the pack (domain config), the map,
   relevant retrieved policy/rule passages, memory about the destination, fleet state.
3. **Intent (LLM, C1/C2 only).** Bedrock turns prose into a **task spec** *or* a
   **clarification question**. Vague requests ("a pump to 2B") trigger a question; the
   answer is folded back in. A deterministic grounding step (`_ground`) repairs anything the
   model dropped (named source, urgent wording, stated deadline).
4. **Allocation proposal.** A proposal names a cluster + robot(s) and a route. In C1/C2 the
   LLM proposes; the **classical allocator** (`cloud/allocator.py`, nearest free eligible
   robot) is always available as the pre-filter and fallback. In C3 the allocator *is* the
   proposer.
5. **Rule Checker (always).** `cloud/checker.py` re-derives every fact itself (never trusts
   the proposer) and runs the pack's rules through a tiny safe condition language
   (`cloud/rules.py`, no `eval`). It returns **all** violations so the model gets one
   informed retry. **The cloud never issues a lease the checker has not approved.**
6. **Lease commit.** `cloud/leasebuilder.py` builds the lease from the checked allocation and
   pack templates; `cloud/leases.py` commits it atomically (claiming the robot and any
   exclusive zones). Validated against the schema before commit.
7. **Hand-off down.** The lease is published to the owning cluster (MQTT/IoT topic). The edge
   reads it, checks in, and starts executing subtasks on robots.
8. **Edge autonomy.** The supervisor runs the robot within the lease. It renews on check-in.
   It handles local blockages itself (retry → detour inside the lease).
9. **Escalations up** (`POST /escalations`, or via IoT Core → SQS in production):
   - `CLARIFY` — needs a human/rule answer.
   - `UNEXPECTED_EVENT` — a closure/gate/infection the cloud must record on the **shared
     map** and re-plan around (`cloud/react.py`, `cloud/mapstate.py`).
   - `OUTPUT_READY` — a leg or task finished, with a `result_ref` (e.g. an S3 photo).
10. **Re-plan / hand-over.** On a blockage the cloud issues lease v(n+1) avoiding the newly
    blocked zones, same robot if possible, else another free robot of the cluster, else a
    cross-wing hand-over split into **legs** (leg 1 to a drop point, leg 2 from there by the
    other wing's robot).
11. **Background ticker** (`cloud/ticker.py`) does time-driven work with no message:
    reopen expired closures, requeue tasks whose lease ran out, raise a one-shot
    deadline-miss alarm, and move the tasks of a silent cluster to another cluster.
12. **Summary.** When the result arrives the cloud writes a plain-language summary for the
    requester (model writes the sentence only when there is text worth summarising).

Everything decision-relevant is written to a **durable decision log** (FR-20), separate from
an in-memory human-readable **trace** (`cloud/trace.py`) used only for demos. The decision
log is designed to be **replayed byte-for-byte** from a saved seed.

---

## 5. Request → lease safety chain (why it's trustworthy)

The whole system is built so that **a model is never the last word on a physical action**:

- The model *proposes*; the deterministic **Rule Checker** *disposes*.
- The checker re-derives facts from the registry/topology/claims, so a hallucinated
  proposal cannot smuggle in a false premise.
- Safety rules come from the **pack** (domain config, §7) and are enforced regardless of
  what retrieval surfaced for the model's context.
- **Memory never overrides a rule**: a remembered "usual pump for this ward" still goes
  through the checker.
- If the model is **off (C3), down, throttled, or rejected twice**, the *same pipeline*
  continues with hand-written rules / the classical allocator — the system degrades, it
  does not stop (FR-18).

---

## 6. The three evaluation configurations (C1 / C2 / C3)

All three run on **one codebase**, toggled by `NIMBUS_MODE` (and `--system-mode` at the
edge). In code, `LLM_MODES = ("C1", "C2")`; C3 is the no-model path.

| Config | What it is | Where decisions are made |
| --- | --- | --- |
| **C1 (proposed)** | Cloud Bedrock LLM + leases + **edge SLM** (1–3B) | Cloud LLM plans; **edge SLM** handles local tactical decisions during the lease, so a bad WAN link doesn't stall the robot. |
| **C2 (cloud-only)** | Edge is a **thin relay**; no edge model | Every non-reflex decision pays a WAN round-trip to the cloud LLM. |
| **C3 (no-LLM baseline)** | **Structured web forms + EDF scheduler + greedy assignment + A\* routing + hand-written finite-state event rules (retry → detour → operator)** | No model anywhere. A strong classical baseline, not a straw man. |

C3's five pieces and where they live:
1. **Structured web forms** — `server.py` (`GET /ui-config` advertises form options from the
   pack/map; `POST /requests {"form": …}`), `static/index.html` + `static/app.js` (the form
   replaces the free-text box when the server is in C3), and
   `Orchestrator.handle_structured_request` (`cloud/orchestrator.py`) which validates fields
   against the active pack/map and feeds the classical pipeline with no model call.
2. **EDF scheduler** — `cloud/scheduler.py`: priority class first (pack ranks: urgent before
   routine), earliest deadline first within a class, arrival order breaks ties. Decides
   *order*, not allocation; a blocked entry never holds up runnable ones.
3. **Greedy assignment** — `cloud/allocator.py`: nearest free robot among eligible clusters.
4. **A\* routing** — `cloud/topology.py` `shortest_path`: A\* with an admissible,
   position-scaled straight-line heuristic (min observed edge-cost/euclidean ratio). Falls
   back to Dijkstra when a map has no node positions (heuristic = 0). *Note:* only the
   simulation packs (`hospital_sim`, `hospital_sim6`) carry node positions, so A\* only does
   real heuristic work there; the hand-made `hospital` test pack runs it as Dijkstra.
5. **Finite-state event rules** — `_step_rules` in `cloud/orchestrator.py` and the `C3_FSM`
   path in `edge/supervisor/supervisor.py`: wait/retry once → reroute (detour inside the
   lease) → abort to operator.

Ablations named in the plan: **A1** (no leases — unfenced edge past expiry) and **A2** (edge
SLM scale 0.5B/1.5B/3B). A Needle direct-to-cloud path is a separate stretch (E9).

---

## 7. "Packs" — the domain is data, not code

A **pack** (`packs/<name>/`, schema `packs/pack.schema.json`) is the domain configuration
that makes the system hospital-specific without any domain knowledge in the code:

- **items** (with variant relationships, e.g. paediatric vs standard infusion pump),
- **priorities** (ranks + default deadlines) and deadline min/max,
- **topology** (`topology.json`: nodes with `kind` — `ward`/`store`/`corridor`/`gate` — plus
  `pos`, aliases, edges with costs; `kind` is what makes a node a valid **destination** or
  **source** for the C3 form),
- **fleet** (`fleet.json`: clusters and their robots),
- **policies** (restrictions, exempt priority classes) and **scenarios**,
- **prompts** and **task templates**.

Shipped packs: `hospital` (hand-made, used by most tests), `hospital_sim` / `hospital_sim6`
(P1's Gazebo maps — 6 racecars, robots R1–R6, nodes carry real positions), `scale10` (scale
testing), `rescue` (disaster/stretch). The module layer "knows no domain" — e.g. the
scheduler takes `pack.ranks`, the rule engine reads `$spec.item` paths, memory reads the
pack's variant graph.

---

## 8. Module map (what to read for what)

**Cloud (`cloud/`)**
- `orchestrator.py` — the decision pipeline; entry points `handle_request` (prose),
  `handle_structured_request` (C3 form), `replan`, `advance_leg`, `renew`, `complete_summary`.
- `checker.py` — the Rule Checker (deterministic veto).
- `rules.py` — the safe JSON condition language (no eval).
- `allocator.py` — classical nearest-free-robot allocation + `FleetView` (the bundle of
  everything a decision reads).
- `scheduler.py` — EDF + priority scheduler.
- `leases.py` — Lease Manager storage/invariants; `MemoryLeaseRepo` for tests.
- `leasebuilder.py` — build a lease doc from a checked allocation.
- `topology.py` — map graph, A\* routing, destinations/sources, detours.
- `react.py` / `agent.py` — reaction to edge events; bounded agent loop (model may call
  look-up tools, must end in one checked terminal action).
- `mapstate.py` / `policy.py` — shared closed-zone map; policy restrictions (hold vs forbid,
  with exemptions).
- `memory.py` — long-term per-place habits (FR-19); `durable.py` — durable KV for non-lease
  state (Memory/File/Dynamo implementations).
- `retrieval.py` — rule retrieval for context (keyword by default; Bedrock Knowledge Base if
  `NIMBUS_KB_ID`).
- `nlp.py` — deterministic, pack-driven parsing: the offline model stand-in and the
  form/outage path.
- `ticker.py` — time-driven maintenance; `clock.py` — injectable clock (`FixedClock` in tests).
- `survey.py` — survey/inventory-sweep task type (T7), lease schema unchanged.
- `llm.py` — LLM abstraction: `BedrockLLM`, `RuleBasedLLM` (deterministic stand-in), circuit
  breaker. `metrics.py` / `cost.py` + top-level `cost.py` — counters for NFR-1 latency and the
  per-task cost formula.
- `aws_dynamo.py` / `aws_store.py` / `sqs_consumer.py` — AWS adapters (DynamoDB tables, SQS
  consumer that reuses the same handler as HTTP/MQTT so dedup/fencing are identical).
- `trace.py` — human-readable trace (never feeds a decision).

**Edge (`edge/`)**
- `run_supervisor.py` — CLI entry (`--cluster`, `--system-mode C1|C2|C3`, `--slm-mode`,
  robot backend).
- `supervisor/supervisor.py` — the daemon: lease enforcement, SQLite persistence, subtask
  execution, local SLM tactical loop, the C3 FSM.
- `supervisor/{outbox_worker,geofence,parking,artifacts,reconnection}.py` — store-and-forward
  uploads, zone enforcement, safe parking on blockage, camera artifact packaging,
  reconnection protocol.
- `slm/engine.py` — the small-model engine (Ollama / llama.cpp / mock / rules_only).
- `store/local_store.py`, `schemas/validator.py`, `mock/` (sim robots).

**Top-level service**
- `server.py` — the local cloud HTTP server (the same public API the app, operator and edge
  speak); `mqtt_transport.py` — MQTT path; `standin.py` — a stand-in.

**Simulation (`simulation/`)**
- `hospital_world/` — Gazebo Fortress worlds + launch scripts (1/2/6 racecars),
  `ground_robot_sdk/` (navigation, control, Gazebo + mock adapters, sensors). Robots are
  **Ackermann** vehicles (they cannot pivot — relevant to the recent navigation tuning).
- `lite/viewer.py` — the kinematic (Gazebo-less) simulator + live web map on :8090.

**Deployment (`infra/`)** — Terraform (`infra/terraform/`) for the AWS stack; IAM policy
(`infra/iam/`). Production pieces: Bedrock, DynamoDB (leases + state + robot_status), SQS,
IoT Core (edge↔cloud messaging), Fargate (always-on warm containers — chosen over Lambda to
avoid cold-start and hold long-lived connections), S3 (artifacts), CloudWatch (EMF metrics).

**Apps (`static/`)** — `index.html`/`app.js` (requester), `operator.html`/`operator.js`
(operator console), `map.html` (shared map view).

---

## 9. Running it

- **Whole stack, local:** `run_all.sh [auto|kinematic|gazebo] [--system-mode C1|C2|C3]`.
  - `kinematic` — no Gazebo; P1 map + SDK nav + live viewer on :8090.
  - `gazebo` — real Gazebo Fortress (6 racecars), needs `ign` + ROS 2 (see
    `scripts/docker_gazebo.sh`).
  - C2/C3 require `NIMBUS_LOCAL_CLOUD=1` (the deployed cloud's mode can't be switched by the
    launcher). Default pack is `hospital_sim6`; default local cloud is `:8000`.
- **Edge SLM:** `--slm-mode auto|ollama|llama_cpp|mock|rules_only` (`auto` falls back to mock
  for CI; real-inference runs fail closed if the model is missing).
- **Stop:** `stop_all.sh`. **Scripted demo:** `scripts/demo_e2e.py --api <cloud>`.

---

## 10. Experiments and evidence

Two layers of experiments:

- **Cloud "claim" experiments** (`experiments/cloud/c1..c9_*.py`) — each targets a PRD
  requirement and emits a plotted result + a one-line claim. Roughly: c1 understanding/
  clarification, c2 rules+map, c3 memory/habits, c4 checker-under-attack, c5 scheduling,
  c6 re-plan around closures, c7 Bedrock-outage fallback, c8 load/latency, c9 model
  calls+tokens per task (C1 vs C2 vs C3). They run against a stand-in model or real Bedrock
  (`--target local-bedrock`). `lib.py` / `report.py` are the harness.
- **End-to-end experiments** (`scripts/run_e3..e7.py`, `run_p3_claims.py`) mapped to the PRD:
  - **E1** benchmark edge models on fixed hardware (speed, valid outputs).
  - **E2** offline decision quality (edge model vs cloud LLM vs rules; context reuse).
  - **E3** C1 vs C2 vs C3 on tasks T1–T8 (success, deadlines, cost/task).
  - **E4** replay real network traces (tc/netem) as the WAN worsens.
  - **E5** failures: outages, edge shutdown, Bedrock unavailable (recovery, lost results).
  - **E6** repeated/outdated/re-ordered messages, selective sharing, IAM audit, byte-for-byte
    replay.
  - **E7** MIMIC-based load with 3–10+ robots (waiting times, missed deadlines, cost, replay
    at scale).
  - **E8/E9** physical PiCar-X / disaster tasks / Needle direct-to-cloud (stretch).
- **Traces** (`traces/*.csv`) are real IISc network captures (corridors, lift, basement)
  logged with a Raspberry Pi 5; workload timings derive from MIMIC-IV demo + BurstGPT.
- Results live in `results/`, `P2_Results/`, `P3_Results/`, `results/final/`.

The git history shows several of these already run against **real Bedrock Nova Lite** on a
local server (e.g. C7 outage, C8 load p95, C9 model-call ratio, C1 field accuracy).

---

## 11. Current repository state (as reviewed)

**Uncommitted, in-progress work** (on `main`, not yet committed) is the **C3 structured-form
baseline plus an A\* upgrade and robot-navigation tuning**:

- `server.py`, `static/index.html`, `static/app.js` — the C3 form UI + `/ui-config` options +
  the `POST /requests {"form": …}` endpoint (409 outside C3).
- `cloud/orchestrator.py` — `handle_structured_request` (validate form → classical pipeline,
  no model). Also: the step-event path now only calls the LLM in `LLM_MODES`, so C3 uses
  rules directly.
- `cloud/topology.py` — Dijkstra → **A\*** with an admissible heuristic (dormant where a pack
  has no node positions).
- `edge/supervisor/supervisor.py` — C3 FSM touch-ups.
- `simulation/.../navigation.py` — Ackermann steering/corner tuning (steering gains, a
  minimum forward "crawl" so a 0.3 m/s lease command doesn't collapse to mm/s at 90° corners)
  — this is demo-recording tuning, not part of C3 logic.
- `run_all.sh` — `--system-mode` plumbing.

**Tests** (`tests/`, `edge/tests/`): a new `tests/test_c3_structured.py` (added during this
review) covers the C3 form path — orchestrator validation (valid lease with **zero** LLM
calls, deadline defaults, every invalid-field rejection, mode gating) and the HTTP surface
(`/ui-config`, form POST, 400/409, dedup). 32 tests, all passing.

**Full-suite status:** `540 passed, 20 failed`. The 20 failures are **not** from the new
test file (it is isolated). They cluster in the **simulation-pack + hand-over + ticker +
E3 harness** areas and are pre-existing / tied to the in-progress map work. Confirmed
examples:
- `test_sc2_...` (corridor-blocked → west gate closed → re-plan to lease v4) fails for **all**
  configs (C1, C3, model-down) — a general re-plan/map issue where the closed gate leaves no
  alternate route the checker accepts, not a C3-specific bug.
- `test_east_pickup_route_cannot_detour_through_west_wing` returns `BLOCKED` where the test
  expects `INVALID_DESTINATION` (robot-SDK routing semantics).
- Several `test_ms2_*` (efficiency, handover on the Gazebo map, mapview coordinates, survey,
  ticker) and `test_e3_harness` (the three-config behaviour + replay determinism).

**Running tests here:** the machine has a ROS 2 Humble environment that injects incompatible
pytest plugins; run with `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest …`. Interpreter
is `python3` (no `python`).

---

## 12. Design decisions worth knowing (context for ideas)

- **Leased authority, not streamed control** — the thing that makes offline operation both
  possible and safe. Expiry ≪ deadline forces periodic (LLM-free) renewal.
- **LLM proposes, rules dispose** — the model is never trusted for a physical commit; the
  checker re-derives facts and returns all violations for one informed retry.
- **One codebase, three configs** — C1/C2/C3 differ only in *who decides*, so comparisons are
  apples-to-apples on the same traces and seeds.
- **Domain is data (packs)** — no hospital knowledge in the modules; swapping a pack
  re-targets the system (rescue pack exists).
- **Idempotent, version-fenced messaging** — HTTP, MQTT and SQS all funnel through one
  handler, so dedup and stale-rejection are identical on every path (FR-8).
- **Fargate over Lambda** — warm, long-lived connections for the orchestrator; avoids
  per-check-in cold-start cost/latency.
- **Edge model on a lab workstation, not on the robot** — the SLM answers in the supervision
  loop (robot already stopped/waiting), never inside obstacle avoidance, so the extra LAN hop
  is acceptable and the robot's compute stays free for navigation.
- **Deterministic replay + decision log** — every decision is reconstructable from a seed
  (FR-20, E6).

---

## 13. Open questions / seams for brainstorming

These are genuine soft spots and extension points, surfaced for ideation (not all are bugs):

1. **The SC2 re-plan failure across all configs.** When the west gate closes mid-leg, the
   re-plan finds no route the checker approves (lease v4 never issues). Is this a map
   connectivity gap in `hospital_sim`, a checker rule that's too strict on a closed exclusive
   zone, or a missing cross-wing hand-over fallback? Fixing it cleanly matters for the T5
   ("gate closed: cross-wing hand-over") story.
2. **A\* is dormant on most packs.** Only the sim packs have node positions; `hospital`,
   `scale10`, `rescue` run it as Dijkstra. Should positions be added to those packs so the
   "A\* routing" claim is exercised everywhere, or is Dijkstra-equivalence acceptable to
   state explicitly?
3. **C3 and memory.** `handle_structured_request` currently calls `_remember` on a successful
   lease, but C3 forms are unambiguous and memory is never *read* in C3 — arguably the
   no-model baseline should carry no habit memory at all, for a cleaner ablation.
4. **C3 form expressivity.** The form only accepts pack-typed destinations (wards) and
   sources (stores); corridors/gates aren't selectable. Fine for deliveries, but limits
   "deliver to/through an arbitrary zone" scenarios without a pack edit.
5. **Edge SLM realism (A2 ablation).** How much does edge-model scale (0.5B/1.5B/3B) actually
   change C1's win over C2 under degraded WAN? The harness exists; the sweep is the interesting
   part.
6. **Cost model is symbolic.** `cost.py` records raw counters; prices stay symbolic and the
   per-task division happens in tooling. A concrete price set would turn the cost comparisons
   into dollar figures.
7. **Scale behaviour (E7).** 3→10+ robots with MIMIC arrival timing — where do waiting times
   and missed-deadline ratios bend, and does the single-cloud lease manager become the
   bottleneck?
8. **Failure taxonomy (E5).** Outage vs edge-shutdown vs Bedrock-down are handled by the same
   degrade-to-rules path; are there failure combinations (e.g. edge down *and* gate closed)
   that aren't covered?

---

## 14. Fast orientation checklist (for a reader going into the code)

- Want the **decision logic**? → `cloud/orchestrator.py` + `cloud/checker.py` + `cloud/rules.py`.
- Want the **lease guarantees**? → `cloud/leases.py` (+ `cloud/aws_dynamo.py` for the real impl).
- Want **C3 specifically**? → `handle_structured_request` + `_step_rules` in
  `cloud/orchestrator.py`, `cloud/allocator.py`, `cloud/scheduler.py`,
  `cloud/topology.py::shortest_path`, `edge/supervisor/supervisor.py` (`C3_FSM`), and
  `static/` + `server.py` for the form.
- Want the **edge**? → `edge/supervisor/supervisor.py` + siblings.
- Want to **run a scenario**? → `run_all.sh` + `scripts/demo_e2e.py`.
- Want the **requirements mapping**? → `PRD.md` §6–7; results under `results/final/`.
- Want the **message formats**? → `docs/EDGE_CLOUD_PROTOCOL.md`, `contracts/*.schema.json`.
