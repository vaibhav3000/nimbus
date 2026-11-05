# Integration change log, by owner

Everything below was done in the integration pass that made cloud ↔ edge ↔ simulator run together.
Nothing was deleted: removed files are in `archive/` (see the end). All 467 tests pass
(`python -m pytest -q` = 407 cloud/P4 + 60 edge).

## Person 1 — Simulation (`simulation/`)
| Change | Why |
|---|---|
| **No existing P1 file was modified.** | The map, `hospital_six_racecars.sdf`, topics (`/robot_N/...`), world name (`hospital`) and spawn poses were checked against what P2's edge and P3's `hospital_sim6` pack expect: they agree (R1-R6 = robot_1..6, same spawn nodes). |
| **Added** `simulation/lite/viewer.py` + `viewer.html` (click a node = close/reopen that zone as an obstacle) | A Gazebo-less live view of P1's hospital map (uses P1's `topology/` and `hospital_topology.png`): the six robots move live, with the cloud's tasks/leases beside them. Used when Gazebo is not installed. |

## Person 2 — Edge (`edge/`)
| Change | File | Why |
|---|---|---|
| `--sim-mode auto\|gazebo\|kinematic` | `run_supervisor.py`, `supervisor/supervisor.py` | `--robot-backend sim` used to die with "No module named rclpy" wherever ROS was missing, so every task FAILED. `kinematic` runs P1's own SDK navigation on a kinematic stand-in (clearly labelled, never presented as Gazebo); `gazebo` still fails closed; `auto` picks Gazebo only if ROS 2 is importable. |
| Kinematic robots stream poses to the viewer | `mock/sim_robot.py` | So robots can be seen moving without Gazebo (only when `NIMBUS_SIM_VIEWER_URL` is set). |
| Edge polls `GET /outbox?active=1` | `supervisor/supervisor.py` | After an edge restart it re-ran every old, already-completed lease from the cloud's outbox history. |
| Delivery leases no longer treated as surveys | `supervisor/supervisor.py` | Every lease ends in `report`, so every delivery produced a `survey.json` result instead of `delivery-proof.jpg`. A lease with a `delivery_proof` photo is a delivery. |
| De-duplicated `Notice received: BUNDLE` spam; heartbeat log is one line | `supervisor/supervisor.py` | The whole outbox was re-printed every 2 s. |
| Test updated for the new outbox URL | `edge/tests/test_prd_person2_compliance.py` | Mock cloud now accepts `/outbox?...`. |
| Blocked corridor now reaches the cloud (FR-4/FR-9, SC-2) | `supervisor/supervisor.py` | A "detour" through the blocked zone itself, or a detour that is also blocked, ended in a silent FAILED. It now sends UNEXPECTED_EVENT; verified live: cloud marks the zone closed, tries a re-plan, alerts the operator when no route is left. |
| Kinematic obstacles are per route zone | `mock/sim_robot.py` | Any injected obstacle used to shrink every robot's range sensor (all robots 'blocked'). Now a leg is blocked only if a zone on its route is closed. Zones can be closed by clicking them in the viewer. |
| Generated run output moved out of the source tree | `edge/artifacts/`, result `.md` files → `archive/edge_generated/` | Runtime output; `edge/artifacts/` is now git-ignored. |

## Person 3 — Cloud decision plane (`cloud/`, `packs/`)
| Change | Why |
|---|---|
| **No P3 code changed.** | Its tests all passed and SC-1 / T1 behaved to the PRD in the live run. |
| `run_all.sh` starts the cloud with `NIMBUS_PACK=hospital_sim6` | P1's six-racecar map with robots R1-R6 as the edge reports them (3 per wing). |
| Planning docs (`docs/superpowers/…`, `context/`) → `archive/docs/` | Not needed to run. |

## Person 4 — API, apps, infra (`server.py`, `static/`, `store.py`, `infra/`)
| Change | Why |
|---|---|
| `GET /outbox?active=1` hides lease messages of finished tasks (stop leases are always kept). Plain `GET /outbox` is unchanged. | Stops restarted edges replaying finished work; keeps the full history for the replay/FR-20 tests. |
| `infra/terraform` untouched | Out of scope for this pass, as requested. |
| `HANDOFF.md`, `docs/*`, root scratch scripts → `archive/` | Not needed to run. |

## Shared / repo level
* `run_all.sh`, `stop_all.sh`, `scripts/docker_gazebo.sh`, `scripts/demo_e2e.py` — one-command start/stop, Docker+Gazebo launcher, scripted SC-1 demo.
* `pyproject.toml` runs `edge/tests` as well as `tests`.
* `README.md` rewritten as a short run guide. PRD and roadmap copied to `docs/`.
* `demo/nimbus_demo.mp4`: screen recording of the live run (two requests, robots R2 → pickup → ward, lease panel).

## Second pass (organise)
Also moved to `archive/`: `aws_tools/` (aws_verify, bedrock_hello, smoke_live, build_push.sh and their two skipped tests), `sim_boilerplate/` (upstream CODE_OF_CONDUCT, CONTRIBUTING, cross-group example), `edge_generated/benchmark_notes/`, `root_scratch/demo_ms2.py`.
Live checks run on the kinematic sim: SC-3 (ward isolation: 2 extraction tasks created and both completed by robots), SC-2 corridor closure (edge escalates, cloud closes the zone and alerts the operator because no alternative route exists in the east wing).

## What is in `archive/` (all moved with `git mv`, restorable)
`root_scratch/` one-off scripts (pose dumps, page printers, ollama launcher, old start scripts, dot_scratch) ·
`docs/` all former `docs/`, `context/`, `REUSE.md` · `planning/` HANDOFF, ASSUMPTIONS, GLOSSARY, SPEC/PLAN files ·
`data_snapshots/` old `data_backup*` · `edge_generated/` benchmark/test result notes and delivery-proof artifacts.
Kept on purpose because tests import them: `standin.py`, `mqtt_transport.py`, `scripts/edge_sim.py` (a fake edge used only by cloud tests).

## Not verified here
The sandbox has no Docker daemon, ROS 2 or Gazebo, so the **real Gazebo path was not run**. I checked statically that
the topic names, world name, spawn poses and robot ids line up across P1, P2 and P3, and ran the identical
cloud → lease → edge → SDK navigation path against the kinematic stand-in. Terraform/AWS were not touched or run.
