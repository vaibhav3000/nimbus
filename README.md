# Nimbus — LLM-orchestrated robot fleets with leased authority (DS252, PRD v5.0)

A staff member types a request → the **cloud** (`server.py`, `cloud/`) clarifies it, plans, rule-checks and issues a
**lease** → an **edge supervisor** per wing (`edge/`) runs the lease on its three robots → robots drive in the
**simulator** (`simulation/`, P1's hospital) → the result returns to the cloud and the app.
PRD: `docs/PRD_v5.pdf`. Who changed what in the integration pass: `CHANGES_BY_PERSON.md`.

## Run it (3 commands)
```bash
pip install -r requirements.txt
./run_all.sh            # cloud + east-wing edge + west-wing edge + simulator   (stop: ./stop_all.sh)
python3 scripts/demo_e2e.py     # sends SC-1 ("Need an infusion pump at 2B-12, urgent") + a West-wing request
```
In the viewer, click a node to close it (obstacle) and watch the edge escalate.
Then open **http://127.0.0.1:8000** (requester app; also `/operator`, `/map`) and watch robots move at
**http://127.0.0.1:8090** (no Gazebo installed) or in the Gazebo window.

`./run_all.sh` auto-selects real Gazebo when `ign` + ROS 2 are present, else the kinematic stand-in
(same P1 map, same SDK navigation). Force one with `./run_all.sh gazebo` / `./run_all.sh kinematic`.

## Real Gazebo (Docker image)
```bash
docker pull kautuk05/ds252-icc:gazebo_hospital
./scripts/docker_gazebo.sh               # GUI through X11/WSLg
HEADLESS=1 ./scripts/docker_gazebo.sh    # no window
```
It mounts this repo at `/root/nimbus`, starts Gazebo with the six racecars, then the cloud and both edges
(`--robot-backend sim --sim-mode gazebo`, which fails closed if Gazebo topics are missing: see `logs/`).
Debug a failing edge: `bash scripts/diagnose_edge.sh east-wing`.

## Layout
```
server.py store.py cost.py contracts*  cloud API, apps backend, decision log        (P4)
cloud/ packs/                          LLM orchestrator, allocator, rule checker, leases (P3)
edge/                                  supervisor, SLM, local store, robot drivers   (P2)
simulation/hospital_world/             Gazebo world, 6 racecars, SDK, topology       (P1)
simulation/lite/                       Gazebo-less live viewer
static/                                requester app, operator console, map
infra/terraform/                       AWS (not part of the local run)
scripts/ tests/ evaluation/ results/   experiments (E1-E7) and tests
archive/                               everything not needed to run (restorable)
```
Tests: `python -m pytest -q`. Evaluation modes: `NIMBUS_MODE=C1|C2|C3 ./run_all.sh`.
Locally the cloud LLM is a deterministic rules stand-in; set `NIMBUS_BACKEND=aws NIMBUS_LLM=bedrock` for Bedrock.
