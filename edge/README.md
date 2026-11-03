# Nimbus — Edge Tier & Local Autonomy (Person 2)

Welcome to the **Edge Tier** component of project **Nimbus** (Leased Authority for Agentic Ground-Robot Navigation across Edge and Cloud).

This folder contains the complete runtime, supervisor, tactical reasoner (SLM), local durable store, mock robot driver, unit tests, and Experiment E1 benchmark suite for Person 2's scope.

---

## Folder Structure

```
edge/
├── supervisor/
│   ├── supervisor.py          # Edge Supervisor loop: boundary enforcement (FR-5), safe park (FR-6), REST sync
│   ├── geofence.py            # Spatial authority & forbidden zone geofence validator
│   ├── parking.py             # Invariant 4 safe parking recess engine
│   ├── artifacts.py           # Camera artifact packaging & cryptographic SHA-256 proof (FR-9)
│   ├── outbox_worker.py       # Store-and-forward WAN retry worker (FR-10, NFR-2)
│   └── reconnection.py        # Reconnection summary & fleet reconciliation engine (FR-12)
├── slm/
│   └── engine.py              # Tactical SLM Engine (Ollama / llama.cpp / mock / rule fallback)
├── store/
│   └── local_store.py         # Local SQLite store & outbox store-and-forward (FR-8, FR-10, NFR-6)
├── mock/
│   └── mock_robot.py          # Robot primitive simulator with Gazebo hospital topology waypoints
├── schemas/
│   ├── lease.schema.json      # Draft 2020-12 wire schema for cloud leases
│   ├── robot_subtask.schema.json
│   ├── escalation.schema.json
│   ├── robot_cmd.schema.json
│   └── validator.py           # Local jsonschema validator
├── benchmarks/
│   ├── e1_benchmark.py        # Experiment E1 benchmark script (latency & JSON validity)
│   └── e1_results.md          # Official benchmark results on NVIDIA Tesla T4 GPU (p95 = 0.495 s)
├── tests/
│   ├── test_edge_tier.py              # Unit & boundary tests (100% passing)
│   ├── test_boundary_and_parking.py   # Spatial geofence & safe recess tests
│   ├── test_escalation_pipeline.py    # CLARIFY, UNEXPECTED_EVENT, OUTPUT_READY tests
│   ├── test_outbox_resilience.py      # Store-and-forward WAN resilience tests
│   └── test_tactical_autonomy.py      # SLM tactical actions & detour boundary tests
├── demo_milestone1_t1.py      # Interactive end-to-end demo for Task T1 routine delivery
├── run_supervisor.py          # Edge Supervisor continuous daemon with live execution trace streaming
├── SPEC-P2.md                 # Person 2 Edge Tier architecture specification
└── PLAN-P2.md                 # Milestone delivery plan & verification matrix
```

---

## Quickstart & Verification

### 1. Run Complete Test Suite
From the repository root:
```bash
python -m pytest edge/tests/ -v
```
All 45 automated unit & integration tests across 7 suites verify:
- **`test_edge_tier.py` (7 tests)**: Ingestion & Draft 2020-12 schema validation (FR-5), monotonic deduplication (FR-8), forbidden zone rejection, expiry watchdog safe parking (FR-6), topology waypoint integration, heartbeat & check-in resilience.
- **`test_boundary_and_parking.py` (9 tests)**: Geofence validator, metric coordinates, intermediate forbidden waypoint interception, elevator/doorway/corridor parking prohibition (Invariant 4), multi-robot safe parking.
- **`test_escalation_pipeline.py` (7 tests)**: `CLARIFY` on ambiguity (FR-9 Table 2), `UNEXPECTED_EVENT` on blockage, `OUTPUT_READY` camera artifact packaging & SHA-256 hash, Cloud re-planning.
- **`test_outbox_resilience.py` (5 tests)**: Store-and-forward SQLite persistence (FR-10), 10-minute WAN outage simulation (zero data loss), exponential backoff, reconnection summary.
- **`test_tactical_autonomy.py` (6 tests)**: Local wait on transient obstacle, local detour around persistent obstacle (FR-4), robot swapping on hardware fault, SLM timeout safety fallback, boundary guard intercepting unauthorized SLM detours.
- **`test_c2_baseline.py` (4 tests)**: Baseline C2 (Cloud-Only Relay) event forwarding (Task 3.4), hardware fault escalation, C1 vs C2 comparative benchmark, and C3 (Deterministic Rules) mode.
- **`test_failover_takeover.py` (2 tests)**: Edge node failover & standby takeover within < 1.0s (FR-7), zero duplicate physical commands executed, outbox queue persistence across crashes.
- **`test_sim_robot_adapter.py` (5 tests)**: Adapter integration with Person 1 ground_robot_sdk, goto navigation, photo capture, beep chime, state reports.

### 2. Run Edge Supervisor Daemon (Live Real-Time Streaming)
Run the Edge Supervisor daemon for a specific cluster (e.g. `east-wing` or `west-wing`):
```bash
python edge/run_supervisor.py --cluster east-wing --system-mode C1
```
Modes: `--system-mode C1` (Edge SLM, default), `C2` (Cloud-Only Relay Baseline), or `C3` (Deterministic Rules Baseline).
By default, the daemon streams detailed real-time execution steps for each ingested lease (use `--quiet` to suppress step traces). Use `--once` for a single poll pass.

### 3. Run Benchmarks
- **Experiment E1 (Latency & Schema Validity)**:
  ```bash
  python edge/benchmarks/e1_benchmark.py qwen2.5:1.5b
  ```
- **Experiment E2 (Offline Decision Quality across 40 Scenarios)**:
  ```bash
  python edge/benchmarks/e2_benchmark.py
  ```
  *(Verified: Edge SLM achieves **97.5% accuracy** vs $\ge 80\%$ target; Rule baseline achieves **95.0% accuracy**).*

### 3. Run Milestone 1 Task T1 Demo
Demonstrates routine hospital delivery (Central Supply -> Ward 2B Bed 12) executed under lease authority:
```bash
python edge/demo_milestone1_t1.py
```

### 4. Run Experiment E1 SLM Benchmark
Profiles local SLM decision latency and JSON syntax compliance:
```bash
python edge/benchmarks/e1_benchmark.py qwen2.5:1.5b
```
*(Verified on Tesla T4 GPU: p95 latency = **0.495 s** vs NFR-1 target <= 10.0 s, 100% JSON validity).*

---

## Integration Interfaces

- **With Cloud (Person 3 / 4):**
  - Cloud posts leases to `/outbox` or drops into `data/outbox.jsonl`.
  - Edge supervisor periodically polls `GET /outbox`, processes tasks, and forwards completed reports or escalations to `POST /escalations`.
- **With Robot (Person 1):**
  - Edge supervisor dispatches concrete commands using `MockRobot` primitives (`go_to`, `wait`, `photo`, `beep`, `stop`, `report`).
  - When Person 1's ROS 2 bridge / Gazebo stack is active, `MockRobot` is swapped with `Ros2Robot` publishing to `/nimbus/robot_cmd`.
