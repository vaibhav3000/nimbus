# Nimbus — LLM-Orchestrated Robot Fleets with Leased Authority (DS252, PRD v6.0)

A clinical staff member submits a request → the **cloud** (`server.py`, `cloud/`) clarifies ambiguity, plans waypoints, rule-checks invariants, and issues a cryptographically bounded **lease** → an **edge supervisor** per hospital wing (`edge/`) executes the lease on its three ground robots → robots drive in the **simulator** (`simulation/`, P1's hospital world) → results and delivery-proof telemetry return to the cloud.

PRD: `docs/PRD_v6.md` (and `docs/PRD_v5.pdf`). Integration details: `CHANGES_BY_PERSON.md`.

---

## 1. Quick Setup & Run Stack

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Launch complete stack (Cloud + East Wing Edge + West Wing Edge + Simulator)
./run_all.sh            # Auto-detects environment (stop anytime with: ./stop_all.sh)

# 3. Dispatch end-to-end clinical mission (Scenario SC-1)
python3 scripts/demo_e2e.py
```

### Access Dashboards & UI
* **Requester App**: [http://127.0.0.1:8000](http://127.0.0.1:8000) (Staff dispatch interface)
* **Operator Console**: [http://127.0.0.1:8000/operator](http://127.0.0.1:8000/operator) (Fleet monitoring and zone closures)
* **Hospital Map View**: [http://127.0.0.1:8000/map](http://127.0.0.1:8000/map) (Real-time topological asset tracking)
* **Live Kinematic Viewer**: [http://127.0.0.1:8090](http://127.0.0.1:8090) (Interactive node obstruction testing)

---

## 2. Evaluation Reproducibility: One Command Per Experiment

Every experiment in the evaluation rubric (E1–E6) is reproducible with a single standalone command:

| Exp | Description | Primary Target | Single Reproduction Command | Key Output Artifact |
| :---: | :--- | :--- | :--- | :--- |
| **E1** | **Edge SLM Benchmarking** | NFR-1 (Edge p95 $\le 10$s, $\ge 95\%$ valid) | `python3 edge/benchmarks/e1_benchmark.py qwen2.5:1.5b` | `results/e1_models.csv` |
| **E2** | **Offline Decision Quality** | FR-4 / FR-13 ($\ge 80\%$ accuracy) | `python3 edge/benchmarks/e2_benchmark.py`<br>`python3 scripts/score_e2.py --config C1` | `data/e2_situations.json`<br>`results/e2_edge.csv` |
| **E3** | **End-to-End Orchestration** | T1–T8 on C1/C2/C3 (NFR-4 cost ratio $\le 0.5$) | `python3 scripts/run_e3.py` | `results/e3/results.csv`<br>`results/e3/*.svg` |
| **E4** | **Network Trace Replay** | FR-6, NFR-2 (Link degradation recovery) | `python3 scripts/run_e4.py`<br>*(Optional Linux netem: `./netem.sh mild_wan`)* | `results/e4.csv`<br>`results/plots/e4_recovery_plot.svg` |
| **E5** | **Edge Failure Resiliency** | FR-7, FR-10, FR-12 (Crash takeover $\le 30$s, 0 lost) | `python3 -m pytest edge/tests/test_outbox_resilience.py edge/tests/test_failover_takeover.py edge/tests/test_boundary_and_parking.py -v` | `results/e5_edge.csv` |
| **E6** | **Protocol Replay & Audit** | FR-5, FR-8, NFR-5 (0 duplicates, synthetic only) | `python3 scripts/inject_messages.py`<br>`python3 scripts/audit_data.py` | `results/e6_injection.txt`<br>`evaluation/manifest.json` |

To run the entire unit and integration test suite across all subsystems:
```bash
python3 -m pytest -q
```

---

## 3. Real Gazebo Physics Execution (Optional Docker Container)

When running on hardware with X11 / Wayland / WSLg graphics:
```bash
# Pull official hospital image:
docker pull kautuk05/ds252-icc:gazebo_hospital

# Launch with 3D GUI window:
./scripts/docker_gazebo.sh

# Or launch in Headless mode (no GUI window):
HEADLESS=1 ./scripts/docker_gazebo.sh
```

---

## 4. Repository Layout

```
├── contracts/               # Wire schemas (lease, escalations, robot commands)
├── cloud/                   # Cloud orchestrator, allocator, rule checker (Person 3)
├── edge/                    # Edge supervisor daemon, SLM engine, SQLite store (Person 2)
│   ├── benchmarks/          # E1 & E2 standalone evaluators
│   ├── supervisor/          # Geofence, parking, outbox, reconnection
│   └── tests/               # 60 automated edge compliance tests
├── simulation/              # P1 hospital world, Gazebo models, kinematic viewer
├── static/                  # Web app frontends (requester, operator, map)
├── scripts/                 # Experiment runners (run_e3.py, run_e4.py, fault_harness.py)
├── traces/                  # IISc campus empirical network traces (corridor, lift, basement)
├── results/                 # Consolidated evaluation artifacts (e1_models, e2_edge, e4, e5_edge)
├── P2_Results/              # Dedicated Person 2 submission bundle
└── docs/                    # PRD v6.0, roadmap, architecture specifications
```
