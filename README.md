# Nimbus

**Robots that understand plain-language requests, share one picture of a building, and keep working when the
network drops.**

Nimbus runs a fleet of delivery robots spread over several areas of a site, for example the wings of a hospital.
A person types an ordinary request ("Need an infusion pump at 2B-12, urgent"). A language model in the cloud
works out what is meant, asks a question if the request is unclear, and checks every plan against hard rules.
It then hands the job to the site computer that runs that area's robots. That site computer is called the
*edge*. The cloud hands over a time-limited permission called a **lease**. Inside the lease, the edge and its
robots act on their own, even if the internet connection is gone for minutes. They contact the cloud again only
when they need help or have a result.

This repository contains the whole system: the cloud service, the edge software, a simulated hospital with six
robots, the web apps, the AWS deployment, and the experiments that measure all of it against the plan.

- Plan and requirements: [PRD.md](PRD.md)
- How the code is organised (for developers): [dev.md](dev.md)
- Message formats between cloud and edge: [docs/EDGE_CLOUD_PROTOCOL.md](docs/EDGE_CLOUD_PROTOCOL.md)
- Measured results: [results/final/RESULTS.md](results/final/RESULTS.md) (one table for every final experiment)
- How to add your experiment's results: [docs/RESULTS_GUIDE.md](docs/RESULTS_GUIDE.md)

---

## 1. The problem

Many organisations now have robots that move things around: hospitals, warehouses, airports, factories, university
campuses. When those robots must work for people across a large site, four difficulties appear together.

1. **People ask in their own words, and leave things out.** A nurse writes "a pump to 2B", not a form with item code,
   pickup point, destination and deadline. Someone, or something, has to understand the request, notice what is
   missing and ask.
2. **The site keeps changing, and everyone needs the same picture of it.** Corridors get blocked, doors are locked, a
   ward is declared infectious, a robot breaks down. What one robot sees in the east wing can change what a robot in
   the west wing should do. That calls for one shared, central view.
3. **The network is not reliable.** Wi-Fi and mobile coverage drop in lifts, stairwells, shielded rooms and
   basements, often for minutes. A robot that must ask a distant server before every move stops dead in exactly
   those places.
4. **Mistakes are not acceptable.** A robot must never enter an isolation ward with routine equipment, take the last
   paediatric pump to the wrong ward, or keep driving on orders that have been withdrawn.

Today's hospital delivery robots (for example Aethon TUG) mostly follow fixed routes with a fixed menu of jobs. They do
not understand free-form requests, combine what several robots see, or re-assign work when plans change.

### Why not just one computer?

| Option | Why it is not enough on its own |
| --- | --- |
| Everything in the cloud | One shared view, and a large model that understands language well. But every robot decision then needs the internet, so robots freeze in every dead zone (we measure this: configuration C2 below). |
| Everything on site | Works offline. But a model that understands open-ended requests is too large to run on a small site computer, and each wing would have its own partial view of the building. |
| Nimbus: split by tier | **Cloud:** understanding, global planning and shared memory. **Edge:** a small local model with bounded authority that keeps working through outages. **Robot:** navigation and a safety stop that need no network at all. |

### The key idea: leases

A **lease** is the cloud's written permission for one job. It lists the robots, the zones they may and may not
enter, a speed limit, a deadline, an expiry time and a version number. While a lease is valid, the edge decides
locally. Small problems are handled inside the lease: a person in the way means wait, a blocked corridor means take
another permitted route, a broken robot means swap in another robot. When the lease expires, or the cloud issues a
newer version, the old permission is gone, so robots never act on stale orders. The idea comes from classic
distributed-systems leases (Gray and Cheriton, 1989), applied here to robots.

The edge contacts the cloud in only three situations:

| Message | Sent when | Example |
| --- | --- | --- |
| `CLARIFY` | The instruction or situation is too ambiguous for the edge | "Robot sees an unknown object in the corridor: wait, or treat the route as closed?" |
| `UNEXPECTED_EVENT` | The plan no longer works and nothing inside the lease fixes it | "The door at the west south hall is locked" |
| `OUTPUT_READY` | A job produced a result | Delivery photo stored; equipment count done |

### Beyond hospitals

The hospital is our test case. The same structure fits any site with several areas, a shared map and an unreliable
link:

- **Warehouses and factories:** one edge per building or floor, with a central planner for orders.
- **University or airport campuses:** robots in different buildings, sharing what each has seen.
- **Disaster response:** robots exploring a partly collapsed building over a patchy radio link.
- **Mines and remote sites:** coverage can be absent for long periods, so bounded local authority is the only option.

The domain is not hard-coded. Places, items, priorities and rules live in a **domain pack** (a folder of JSON files,
see `packs/`), so the same code runs a hospital pack, a rescue pack or a 10-robot scale pack.

---

## 2. Words used in this project

| Term | Meaning |
| --- | --- |
| **Cloud** | The central service, running on Amazon Web Services (AWS). It talks to people, plans, keeps the shared map and memory, and issues leases. |
| **Edge** / **edge cluster** | A computer on site that runs the robots of one area (here: one hospital wing). Our setup has two: `east-wing` and `west-wing`, with three robots each. |
| **Robot** | A small wheeled robot (simulated here, modelled on the PiCar-X / MIT RACECAR) that follows simple commands: go to, wait, stop, photo, beep, report. |
| **LLM** | Large language model: a model that reads and writes text. The cloud uses **Amazon Nova Lite** through **Amazon Bedrock**, AWS's hosted model service. |
| **SLM** | Small language model (1 to 3 billion parameters) that runs on the edge computer, for example Qwen2.5-1.5B served by Ollama or llama.cpp. It makes local decisions within the lease. |
| **Lease** | Time-limited, versioned permission for one job (see above). |
| **Escalation** | A message from the edge asking the cloud for help or reporting a result (`CLARIFY`, `UNEXPECTED_EVENT`, `OUTPUT_READY`). |
| **Rule Checker** | Deterministic code in the cloud that verifies every plan the model proposes: right cluster, free and working robot, stock available, no forbidden zone, a real route. No lease is issued without it. |
| **Fallback** | When the cloud model is unavailable, plain rules take over (nearest free robot, fixed priority order), and users are told the system is in reduced-autonomy mode. |
| **Domain pack** | The JSON description of a site: map, places, items, priorities, rules and prompts. |
| **C1, C2, C3** | The three configurations we compare. **C1** is Nimbus (cloud LLM, leases, edge SLM). **C2** is cloud-only: the edge relays every decision to the cloud model. **C3** uses no model at all (forms and fixed rules). |
| **Gazebo** | A robot simulator. Our hospital world and six robots run in Gazebo Fortress; a lighter "kinematic" simulator runs the same map without it. |
| **Decision log** | An append-only record of every decision: who decided (cloud, edge, rule), on what input, with what output, under which lease version. |

---

## 3. How a request flows

This example is scenario SC-1 from the plan: "Need an infusion pump at 2B-12, urgent."

1. **Request.** A nurse types the request into the requester app (a web page served by the cloud).
2. **Understand.** The cloud model turns the text into a task (item, destination, priority, deadline). Code then
   checks the result against the words actually used. A place nobody named is asked for, not guessed.
3. **Ask if needed.** 2B is a paediatric ward, and a hospital rule (SOP-PAED-01) says a standard pump there needs
   confirmation. The cloud asks "Do you need a paediatric pump?", and the nurse answers in the app.
4. **Plan and check.** The code ranks which robots could do the job. If two wings could, the model picks one. The
   Rule Checker verifies the choice. After two rejected proposals, plain rules decide.
5. **Lease.** The cloud issues lease version 1 to the east-wing edge: robot, route, permitted zones, 12-minute
   deadline, expiry.
6. **Local execution.** The edge splits the lease into robot commands. If something gets in the way, the edge's small
   model decides within the lease: wait, detour or swap robots.
7. **Help only when needed.** If the only corridor is locked, the edge sends `UNEXPECTED_EVENT`. The cloud updates the
   shared map, re-plans (lease version 2) or hands the job to another wing, and warns other affected wings.
8. **Result.** The robot takes a delivery photo, and the edge sends `OUTPUT_READY`. Results are kept on the edge's
   disk until the upload succeeds. The cloud tells the nurse, updates its memory ("2B usually wants the paediatric
   pump") and closes the lease.

If the internet drops at step 6, the robot keeps going under its lease. Messages wait on the edge's disk and are
sent on reconnection. If the lease expires first, the robot parks somewhere safe, never in a doorway or lift.

---

## 4. Architecture

These are the architecture figures from the project plan ([PRD.md](PRD.md)), unchanged.

**Figure 1: components and tier boundaries (cloud, edge clusters, robots).**

![Figure 1: component diagram](docs/figures/fig1_architecture.png)

Data crossing the cloud-edge boundary is limited to leases, escalations and check-ins, typically under 2 KB each.
Results such as photos cross only inside `OUTPUT_READY` messages, never as a raw camera stream.

**Figure 2: nested control loops.** Each loop keeps working without the one above it.

![Figure 2: control loops](docs/figures/fig2_control_loops.png)

**Figure 3: life of a task.** Every event goes to the edge model first, which handles it inside the lease or asks the
cloud.

![Figure 3: life of a task](docs/figures/fig3_task_lifecycle.png)

**Figure 4: AWS deployment** (Terraform in `infra/terraform/`).

![Figure 4: AWS deployment](docs/figures/fig4_aws_deployment.png)

---

## 5. What each part does

### Cloud (`server.py`, `cloud/`, `packs/`)
- **Understands requests** with Amazon Bedrock (Nova Lite) using forced tool calls. Every model answer is checked
  against the requester's own words before it is used.
- **Asks clarifying questions** when details are missing or a rule requires confirmation, and resumes from the answer.
- **Remembers habits** ("2B has confirmed the paediatric pump twice"), so repeat requests are not asked again.
- **Applies plain-language rules** such as "Ward 2C is now isolation; hold routine deliveries, urgent ones can still go
  in", including moving equipment out.
- **Schedules by priority and deadline:** urgent first, earliest deadline first within a class.
- **Proposes and checks allocations:** the model chooses only among candidates the code has ranked, and the Rule
  Checker verifies every choice. Two failed proposals mean plain rules take over.
- **Issues versioned leases**, renews them on check-in and fences outdated versions (an old version gets HTTP 409).
- **Reacts to edge reports:** keeps a shared map of closures, re-plans around blocked zones, gives a failed robot's
  job to another robot, hands work over to another wing, warns only the wings that are affected, and alerts the
  operator when nothing works.
- **Survives model outages** through a circuit breaker and deterministic fallback, and tells users.
- **Records everything:** an append-only decision log, a per-task trace of every model call (prompt, tools, tokens,
  latency), and cost counters for every term of the plan's cost formula.

### Edge (`edge/`)
- **Edge supervisor:** one per wing. It receives leases, splits them into robot commands, watches the robots, and
  enforces the lease (geofence, expiry, version).
- **Edge model:** a small local model (Ollama or llama.cpp) chooses between wait, local detour, robot swap, safe park
  and ask the cloud. A rule engine takes over if the model is slow, unavailable or answers outside the allowed actions.
- **Store-and-forward:** escalations and results are kept in a local SQLite database until the cloud has them.
- **Reconnection summary**, safe parking on expiry, and **takeover** by a standby edge node if one fails.

### Robots and simulation (`simulation/`)
- **Hospital world:** a Gazebo Fortress model of a real hospital floor with six simulated RACECAR robots, cameras and
  range sensors.
- **Robot SDK:** A* path planning on an occupancy map, obstacle avoidance with a 180° range sensor, and a reflex
  safety stop that needs no network.
- **Topology:** the map as a graph of named places and corridors, checked against the walls, plus a
  Gazebo-less **live viewer**. Click a corridor to close it and watch the system react.

### Apps (`static/`)
- **Requester app:** plain-language requests, answers to questions, status and results.
- **Operator console:** robots, leases, alerts, pause and cancel.
- **Map page:** the live map with closures, restrictions and robot positions.

### Infrastructure (`infra/terraform/`)
- AWS in one region: API Gateway, an always-on container service for the cloud, DynamoDB for leases, tasks and
  state, S3 for results and logs, Bedrock for the model, CloudWatch for metrics and alarms, and IAM for access.

---

## 6. Install and run

You need **Python 3.10+** and **git**. Gazebo, ROS 2, an AWS account and a local model are all optional.

```bash
git clone <repository-url> nimbus && cd nimbus
```

```bash
pip install -r requirements.txt
```

### Run everything on your own machine (recommended first)

This starts the cloud locally (with a rule-based stand-in instead of Bedrock), two edge supervisors, and the
kinematic simulator with a live viewer:

```bash
NIMBUS_LOCAL_CLOUD=1 ./run_all.sh kinematic
```

Then:

- open the requester app printed by the launcher (by default http://127.0.0.1:8000)
- watch the robots at http://127.0.0.1:8090
- send the sample requests with `python3 scripts/demo_e2e.py`
- stop everything with `./stop_all.sh` (logs are in `logs/`)

### Variations

| Goal | Command |
| --- | --- |
| Use the real Bedrock model in the local cloud | `pip install -r requirements-aws.txt`, set AWS credentials, then `NIMBUS_LOCAL_CLOUD=1 NIMBUS_LLM=bedrock NIMBUS_BEDROCK_MODEL=apac.amazon.nova-lite-v1:0 ./run_all.sh kinematic` |
| Use a real small model on the edge | Install [Ollama](https://ollama.com), run `ollama pull qwen2.5:1.5b`, then add `--slm-mode ollama` |
| Real Gazebo (Docker image) | `docker pull kautuk05/ds252-icc:gazebo_hospital` then `./scripts/docker_gazebo.sh` (add `HEADLESS=1` for no window) |
| Compare configurations | `NIMBUS_LOCAL_CLOUD=1 ./run_all.sh kinematic --system-mode C2` (cloud-only) or `NIMBUS_LOCAL_CLOUD=1 ./run_all.sh kinematic --system-mode C3` (classical no-LLM baseline) |
| Use the team's AWS deployment | `./run_all.sh` without `NIMBUS_LOCAL_CLOUD` uses the deployed cloud. It is shared, so only use it when you mean to. |

### Tests

```bash
python -m pytest -q
```

On this branch, 15 tests fail. They came with the latest navigation and map changes merged from `main`, where ward 3
was disconnected in the robot map. See [dev.md](dev.md#known-issues).

---

## 7. Experiments and results

The plan (PRD) defines requirements (FR-1 to FR-22, NFR-1 to NFR-7), task scenarios (T1 to T8) and experiments
(E1 to E7). The final experiments are split into three groups of about equal effort:

| Group | Area | What it covers | Code | Results |
| --- | --- | --- | --- | --- |
| C1 to C9 | Cloud | understanding, rules, memory, checker, scheduling, re-planning, model outage, load, model calls | `experiments/cloud/` | `results/final/C*` |
| R1 to R10 | Robot and edge | safety stop, navigation, map, edge model, edge decisions, lease limits, parking, takeover, store-and-forward, message format | `experiments/robot_edge/` | `results/final/R*` |
| N1 to N10 | Network and infrastructure | network traces, outage model, trace replay, duplicate messages, selective sharing, replay, security audit, scale, cost, apps | `experiments/network_infra/` (to be added) | `results/final/N*` (to be added) |

Every experiment writes a folder in `results/final/` containing:

- the raw record of every trial
- a summary table
- plots
- the environment it ran in
- a README with the claim, the plan's pass bar, the measured value and PASS or FAIL

### Cloud experiments, measured

The full table is [results/final/RESULTS.md](results/final/RESULTS.md).

| Experiment | What it showed |
| --- | --- |
| C1, request understanding (FR-13) | Real Bedrock Nova Lite on a local server running the current cloud code, 300 decisions: 98.1% of request details right, 75.0% of vague requests got a question, 98.4% precision. The earlier run on the deployed cloud (`C1_deployed`) scored 94.0% and 72.6%. Recall did not reach the 92.9% the what-if analysis predicted: the 7 misses are all vague pump requests. |
| C2, rules and shared map (FR-17) | Real Bedrock Nova Lite on a local server: 9 of 10 plain-language rules applied fully (FAIL against the 10 of 10 bar). The miss: for "get the spare pumps out" the restriction was set but no pump was moved. Effect on deliveries was right in 7 of 7 checks. 20 of 20 closures were on the map within 2 s (p95 1.60 s), and the next plan avoided them. The stand-in result (10 of 10) stays in `C2_standin`. |
| C3, memory (FR-19) | Real Bedrock Nova Lite on a local server: with memory, 19 of 20 cases were right and 10 of 10 repeat requests were resolved without asking. Without memory, all 10 needed a question. One explicit-choice request was asked about anyway. The stand-in result stays in `C3_standin`. |
| C4, Rule Checker under attack (FR-15) | 0 of 180 invalid proposals approved. 0 invalid leases from 60 requests with an adversarial model. The fallback took over every time it had to. |
| C5, scheduling (FR-14, T8) | Over 113,000 simulated tasks: 0 times did a routine task take a robot while an urgent one waited (first-come-first-served: 25,009). At overload, 0% of urgent deadlines missed against 77%. |
| C6, re-planning (FR-16, T5) | Real Bedrock Nova Lite on a local server, 32 incidents: every new plan avoided the closed zone or the failed robot. Decision time p50 0.84 s and p95 1.58 s against the 30 s bar (the model decided 21 incidents, the rules 11). When no route was left, the operator was alerted. |
| C7, model outage (FR-18) | Real Bedrock Nova Lite on a local server: before the outage the model understood 20 of 20 requests. During the 10-minute outage every one of 14 requests still got a decision from the rules (5 leased, 9 queued for a robot), every user was told, and 14 of 14 tasks leased before the outage finished. No requests were sent after the outage, so the model's return is seen only in the status poll. |
| C8, parallel requests (NFR-1) | Real Bedrock Nova Lite on a local server (not the deployed infrastructure, which is still to be measured): 0 errors at 1 to 64 parallel clients and at 0.5 to 4 requests per second. Worst p95 request-to-decision time 2.13 s against the 30 s bar. Fitted capacity about 45 requests per second. Most of these fixed clear requests probably do not reach the model, so this is mainly the cloud's own request path. |
| C9, model calls (NFR-4) | Real Bedrock Nova Lite on a local server: Nimbus (C1) makes 22% of the cloud model calls of the cloud-only design (C2): 1.18 against 5.27 calls per task over T1 to T5, T7 and T8, and about 2,650 against 7,400 input tokens per task. The plan's bar is 50%. The C2 relay calls are paced 3 s apart; as a burst, Bedrock throttled them. |

Several of these ran with the stand-in model, and each result says so (`source` column). The commands to rerun them
on real Bedrock are in [experiments/cloud/README.md](experiments/cloud/README.md).

### Robot and edge experiments

To be filled: one row per experiment, with a link to its results folder ([how](docs/RESULTS_GUIDE.md)).

| Experiment | PRD | What it showed |
| --- | --- | --- |
| R1, safety stop with the network cut | FR-1 | to be filled |
| R2, navigation with moving obstacles | FR-2 | to be filled |
| R3, map and localisation check | FR-2, FR-5 | to be filled |
| R4, edge model benchmark | E1, NFR-1, FR-3 | to be filled |
| R5, edge handles locally or asks | FR-4 | to be filled |
| R6, stay within the lease | FR-5 | [MockRobot supervisor result](results/final/R6/README.md): 60/60 invalid task commands were prevented before execution (20 forbidden-zone, 20 expired-lease, 20 stale-version); 0 unauthorized task executions. The 20 expiry cases executed only the authorised safe-park command. PASS; this is not physical Gazebo validation. |
| R7, safe parking on expiry or link cut | FR-6 | to be filled |
| R8, edge node takeover | FR-7 | to be filled |
| R9, store-and-forward and reconnection | FR-10, FR-12, NFR-2 | to be filled |
| R10, escalation format and message sizes | FR-9 | [Fixture-level result](results/final/R10/README.md): all 5 expected-valid envelopes were schema-valid and under 2 KB; all 5 malformed envelopes were rejected. PASS: `OUTPUT_READY` carries a result reference rather than embedded artifact bytes, so the 200 KB–2 MB artifact range is not a wire-envelope criterion. |

### Network and infrastructure experiments

To be filled: one row per experiment, with a link to its results folder ([how](docs/RESULTS_GUIDE.md)).

| Experiment | PRD | What it showed |
| --- | --- | --- |
| N1, real network traces | E4 input | to be filled |
| N2, Poisson model of network breakdown | E4 | to be filled |
| N3, trace replay | FR-6, NFR-2 | to be filled |
| N4, duplicate, re-ordered and stale messages | FR-8 | to be filled |
| N5, selective sharing | FR-11 | to be filled |
| N6, audit replay and determinism | FR-20, NFR-6 | to be filled |
| N7, security and privacy audit | NFR-5 | to be filled |
| N8, scalability | NFR-3 | to be filled |
| N9, cost per task | NFR-4 | to be filled |
| N10, requester app and operator console | FR-21, FR-22 | to be filled |

### Joint experiment E3 (T1 to T8 across C1, C2 and C3)

To be filled once the C2 relay is wired into the edge and the navigation fixes are in: success rate, deadlines met,
completion time, model calls and cost per task, for each task and configuration.

---

## 8. Repository layout

```
server.py  store.py  cost.py  contracts.py   cloud HTTP API and web apps, task store, decision log, cost counters
cloud/                                       cloud decision logic: orchestrator, rule checker, leases, scheduler, map, memory
packs/                                       domain packs (hospital, hospital_sim6 = the simulated hospital, rescue, scale10)
contracts/                                   JSON schemas shared by cloud and edge (lease, escalation, decision log)
edge/                                        edge supervisor, small-model engine, local store, robot drivers, edge tests
simulation/hospital_world/                   Gazebo world, robots, robot SDK, topology
simulation/lite/                             Gazebo-less live viewer
static/                                      requester app, operator console, map page
infra/terraform/                             AWS deployment
experiments/cloud/                              cloud experiments C1 to C9 (harness, scripts, report)
scripts/                                     demo, older experiment runners (E1 to E7), audits, tools
evaluation/                                  labelled data sets (synthetic) and the dataset manifest
results/                                     measured results (results/final/ = the final experiments)
tests/                                       cloud and shared tests (edge tests are in edge/tests)
docs/                                        protocol, PRD versions, figures
archive/                                     files no longer needed to run (kept for reference)
```

## 9. Team

Team Nimbus, DS252 Introduction to Cloud Computing (Aug to Nov 2026), IISc: Kautuk Astu, M Vaibhav, Garrepally
Akshaya and C Veera Sai Charan Teja. The work covers four areas: simulation and safety, the edge tier, cloud
orchestration, and infrastructure and logging.
