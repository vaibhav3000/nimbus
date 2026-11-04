# Local Network Fault Injection Harness (Experiments E4–E6)

This document describes the switchable network fault injection harness built for **Person 4** in **Milestone 3**. It enables rigorous evaluation of cloud-edge communication under WAN latency, jitter, packet loss, and link disconnection as required by PRD Section 7 (Experiments E4, E5, and E6).

---

## 1. Overview & Architecture

The harness provides two execution mechanisms:

1. **Pure-Python Streaming Proxy (`FaultProxy`):**
   - A cross-platform, zero-dependency TCP streaming proxy implemented in Python standard library (`scripts/fault_harness.py`).
   - Runs locally on developer workstations (Windows, macOS, Linux) without requiring root/sudo privileges or kernel modules.
   - Transparently intercepts and degrades traffic for both **HTTP REST** (e.g. port `8001` $\rightarrow$ `8000`) and **MQTT** (e.g. port `1884` $\rightarrow$ `1883`).
   - Supports static degradation profiles as well as dynamic **trace-driven replay** at 10 Hz.

2. **Linux `tc`/`netem` Integration:**
   - Command generator and runner for Linux environments using the kernel network emulator (`iproute2 netem`).
   - Ideal for Raspberry Pi nodes, Docker containers, and dedicated testbeds.

```
+---------------------+           +--------------------------+           +---------------------+
|                     |  HTTP     |   Nimbus Fault Proxy     |  HTTP     |                     |
|  Edge Supervisor    |  :8001    |  (scripts/fault_harness) |  :8000    |  Nimbus Cloud API   |
|   / edge_sim.py     | --------> |                          | --------> |     (server.py)     |
|                     |           | • Latency / Jitter       |           |                     |
|                     |  MQTT     | • Packet Loss Rate       |  MQTT     |                     |
|                     |  :1884    | • Partition Disconnect   |  :1883    |  Mosquitto Broker   |
|                     | --------> | • 10 Hz Trace Playback   | --------> |                     |
+---------------------+           +--------------------------+           +---------------------+
```

---

## 2. Quick Start: Local Fault Proxy

### A. HTTP Cloud-Edge Link Degradation

**Option 1: One-command in-process proxy via `edge_sim.py` (simplest):**
```bash
# In terminal 1, start server:
python server.py

# In terminal 2, run edge_sim with built-in fault proxy:
python scripts/edge_sim.py --fault-profile shielded_room --cluster east-wing --complete
```
`edge_sim.py` automatically spins up a background `FaultProxy` on port 8001 pointing to `127.0.0.1:8000`, routes all traffic through it, and cleanly shuts it down on exit.

**Option 2: Standalone Fault Proxy:**
1. Start the Nimbus cloud server on its default port:
   ```bash
   python server.py
   # Listening on http://127.0.0.1:8000
   ```

2. In a second terminal, start the fault proxy on port `8001` targeting port `8000`:
   ```bash
   # Emulate a shielded room / lift (250ms latency, 80ms jitter, 5% loss)
   python scripts/fault_harness.py --listen 8001 --target 8000 --profile shielded_room
   ```

3. Run edge simulation through the degraded proxy port:
   ```bash
   python scripts/edge_sim.py --api http://127.0.0.1:8001 --cluster east-wing --complete
   ```

### B. MQTT Broker Degradation
Proxy MQTT traffic between the edge and a local Mosquitto broker:
```bash
python scripts/fault_harness.py --listen 1884 --target 1883 --profile severe
```
Point MQTT clients to port `1884` instead of `1883` to experience degraded publish/subscribe QoS.

---

## 3. Preset Degradation Profiles

The harness includes preset profiles matching hospital deployment conditions described in PRD Sec. 1–2:

| Profile Name | Latency (ms) | Jitter (ms) | Loss Rate | Description |
| :--- | :--- | :--- | :--- | :--- |
| `healthy` | 0 ms | 0 ms | 0% | Baseline clean local link. |
| `mild_wan` | 60 ms | 15 ms | 1% | Normal campus WAN / inter-wing wireless. |
| `shielded_room` | 250 ms | 80 ms | 5% | Lead-lined radiology ward, elevators, and fire stairwells. |
| `severe` | 600 ms | 200 ms | 15% | High congestion or edge-of-coverage degradation. |
| `basement_outage` | 0 ms | 0 ms | 100% | Complete network partition / outage (refuses connections). |

### Custom CLI Parameters
Override specific parameters directly on the command line:
```bash
python scripts/fault_harness.py --listen 8001 --target 8000 --latency 150 --jitter 30 --loss 0.08
```

---

## 4. Trace-Driven Playback (PRD Sec. 7)

PRD Section 7 specifies replaying 10 Hz RTT and packet loss traces recorded on campus routes. The harness includes sample traces in `traces/`:

- `traces/iisc_corridor.csv`: Baseline walking trace through main building corridors (~20 ms RTT, 0% loss).
- `traces/iisc_lift.csv`: Elevator trip trace entering an enclosed lift shaft at $t=5$s (RTT spikes to 250–500 ms with 5–20% loss), exiting at $t=16$s.
- `traces/iisc_basement.csv`: Complete network outage window between $t=5$s and $t=24$s (100% loss / partition), reconnecting at $t=25$s.

### Running with a Trace
```bash
python scripts/fault_harness.py --listen 8001 --target 8000 --trace traces/iisc_lift.csv
```
The proxy updates its active latency and loss dynamically as the experiment clock advances.

### Trace Format
Trace files are simple CSV files:
```csv
time_sec,rtt_ms,loss_pct
0.0,22.0,0.0
1.0,24.5,0.0
5.0,185.0,8.0
10.0,340.0,12.0
```

---

## 5. Linux `tc`/`netem` Hardware Mode

When deploying on Linux workstations, Raspberry Pis, or Docker containers with root privileges, the harness can generate or directly apply kernel `tc qdisc` rules:

### Print Command
```bash
python scripts/fault_harness.py --tc-script --profile severe --iface eth0
# Output: sudo tc qdisc add dev eth0 root netem delay 600.0ms 200.0ms distribution normal loss 15.00%
```

### Apply Directly (Linux with sudo)
```bash
python scripts/fault_harness.py --tc-apply --profile shielded_room --iface eth0
```

### Clear Rules
```bash
python scripts/fault_harness.py --tc-clear --iface eth0
# Output: sudo tc qdisc del dev eth0 root
```

---

## 6. Environment Variables Reference

The harness can be configured via environment variables for automated test pipelines:

| Variable | Values | Default | Purpose |
| :--- | :--- | :--- | :--- |
| `NIMBUS_FAULT_PROFILE` | `healthy`, `mild_wan`, `shielded_room`, `severe`, `basement_outage` | _(none)_ | Selects pre-configured degradation profile |
| `NIMBUS_FAULT_LATENCY_MS` | Float (e.g. `120`) | `0` | Injected one-way latency in milliseconds |
| `NIMBUS_FAULT_JITTER_MS` | Float (e.g. `25`) | `0` | Uniform random latency jitter in milliseconds |
| `NIMBUS_FAULT_LOSS_RATE` | Float (e.g. `0.05`) | `0.0` | Packet drop probability ($0.0 = 0\%$, $1.0 = 100\%$) |
| `NIMBUS_FAULT_DISCONNECTED` | `1`, `true` | `0` | Simulates complete link partition / outage |
| `NIMBUS_FAULT_TRACE` | File path (e.g. `traces/iisc_lift.csv`) | _(none)_ | CSV trace file for time-series playback |

---

## 7. Experiments Guide & Exact Commands (E4–E6)

This section provides the exact commands for running experiments **E4**, **E5**, and **E6** defined in PRD Section 7.

> [!NOTE]
> **Measurement Origin Note:** Recovery timestamps, packet drop counts, and connection failure metrics in local test runs are produced by the local userspace **`FaultProxy`** (`scripts/fault_harness.py`), **not by a live Linux kernel `tc`/`netem` run** (which is reserved for hardware nodes with root privileges via `--tc-apply`). See [`docs/e4-e5-evidence.md`](e4-e5-evidence.md) for full details.

### Experiment E4: Latency & Jitter Degradation (Tasks T2, T4, T5, T7)

**Goal:** Evaluate system responsiveness, task handover, timeline tracking, and lease renewal under WAN latency and jitter without triggering spurious lease expirations.

**Exact Commands:**
1. Start the Nimbus cloud server:
   ```bash
   python server.py
   # Listening on http://127.0.0.1:8000
   ```
2. Run the edge simulation with injected WAN latency and jitter:
   ```bash
   # Method A: One-liner in-process proxy (60ms latency, 15ms jitter)
   python scripts/edge_sim.py --fault-profile mild_wan --cluster east-wing --complete

   # Method B: Severe corridor/elevator degradation (250ms latency, 80ms jitter)
   python scripts/edge_sim.py --fault-profile shielded_room --cluster east-wing --complete

   # Method C: Dynamic 10 Hz trace playback recorded from IISc campus lift
   python scripts/fault_harness.py --listen 8001 --target 8000 --trace traces/iisc_lift.csv
   python scripts/edge_sim.py --api http://127.0.0.1:8001 --cluster east-wing --complete
   ```
3. **Verification:** Inspect task completion timeline at `GET http://127.0.0.1:8000/tasks/<task_id>/timeline`. Verify that all lease handovers, check-ins, and `OUTPUT_READY` events are recorded successfully with timeline timestamps reflecting injected latency.

---

### Experiment E5: Packet Loss Degradation

**Goal:** Measure system robustness and retry resilience under elevated packet drop rates (1% to 15%) as robots navigate through weak wireless zones.

**Packet-Loss Semantics in Userspace TCP Proxy:**
- A userspace streaming proxy operates above the OS TCP stack and therefore must not drop arbitrary mid-stream bytes, which would puncture holes in the TCP byte stream and corrupt HTTP headers or JSON syntax.
- Instead, `FaultProxy` implements connection-level loss semantics evaluated once per connection upon ingress. If `profile.should_drop()` evaluates to true, the connection is aborted immediately (`shutdown()`/`close()`), incrementing `packets_dropped` and `connections_rejected`.
- Evaluating loss once per connection ensures the observed loss rate strictly matches the configured `loss_rate` (avoiding compound multi-chunk probabilities), while forcing the edge client to exercise honest transport reconnection, retry policies, and store-and-forward outbox buffering without synthetic payload corruption.

**Exact Commands:**
1. Start the Nimbus cloud server:
   ```bash
   python server.py
   ```
2. Run edge simulation through packet loss:
   ```bash
   # Method A: One-liner with severe 15% packet loss
   python scripts/edge_sim.py --fault-profile severe --cluster east-wing --complete

   # Method B: Standalone proxy with custom 10% packet drop rate
   python scripts/fault_harness.py --listen 8001 --target 8000 --loss 0.10
   python scripts/edge_sim.py --api http://127.0.0.1:8001 --cluster east-wing --complete
   ```
3. **Verification:** Check `proxy.stats` upon exit to observe packet drops (`packets_dropped`). Confirm that the edge retries HTTP calls and that tasks complete without unhandled crashes.

---

### Experiment E6: Network Partition (Basement Outage) and Reconnection Recovery

**Goal:** Validate store-and-forward outbox buffering (FR-10, NFR-2) when the edge loses cloud connectivity, followed by automatic recovery and zero message loss upon link restoration.

#### A. Interactive Step-by-Step Procedure:
1. **Start the Nimbus Cloud API:**
   ```bash
   python server.py
   ```

2. **Start Fault Proxy in `basement_outage` mode (100% partition):**
   ```bash
   python scripts/fault_harness.py --listen 8001 --target 8000 --profile basement_outage
   ```

3. **Trigger an Edge Escalation / Action during Outage:**
   ```bash
   python scripts/edge_sim.py --api http://127.0.0.1:8001 --cluster east-wing --block E3 --once
   ```
   *Expected behavior:* The HTTP connection is dropped/refused by the proxy. `EdgeSim` / `EdgeSupervisor` catches the network disconnection, logs the failure, and buffers the escalation message in the local store-and-forward outbox (`outbox_buffer` / SQLite `outbox` table). The task on the cloud remains in its current state without corruption.

4. **Restore Connectivity (Reconnect Link):**
   Stop the outage proxy (`Ctrl+C`) and restart in healthy mode (or hot-swap profile):
   ```bash
   python scripts/fault_harness.py --listen 8001 --target 8000 --profile healthy
   ```

5. **Flush & Replay Buffered Messages:**
   ```bash
   python scripts/edge_sim.py --api http://127.0.0.1:8001 --cluster east-wing --once
   ```
   *Expected behavior:* `EdgeSim.flush()` (or `EdgeSupervisor.flush_pending_escalations()`) re-transmits all buffered outbox messages to `POST /escalations`. All buffered messages receive `200 OK`, the buffer is drained, and the cloud issues lease v2 or completes the task with zero data loss.

#### B. Automated Offline-Quality Smoke Test:
Run the automated pytest test suite covering partition hold-and-replay:
```bash
pytest tests/test_fault_harness.py -k test_offline_quality -v
```
This runs both:
- `test_offline_quality_edge_sim_hold_and_replay_on_reconnect`
- `test_offline_quality_edge_supervisor_hold_and_replay_on_reconnect`
which inject `basement_outage`, verify messages are held locally in outbox, reconnect the proxy to `healthy`, and verify 100% successful delivery.

---

## 8. Operator Console Live Fault Indicator (Demo Mode)

During a live evaluation or demo, the operator console at `http://127.0.0.1:8000/operator.html` provides visual confirmation of the link state directly inside the **Command Floor Spatial Grid HUD**:

- **Real-Time Fault Badge:** When `FaultProxy` is running or a profile is set, the HUD header displays the active profile and metrics:
  - `LINK: MILD_WAN (60ms ± 15ms, 1% loss)` (steel/blue warning tag)
  - `LINK: SHIELDED_ROOM (250ms ± 80ms, 5% loss)` (amber warning tag)
  - `LINK: SEVERE (600ms, 15% loss)` (orange alert tag)
  - `LINK: PARTITIONED (BASEMENT_OUTAGE)` (pulsing red urgent tag)
- **Live Telemetry Numbers:** View real-time degradation metrics directly in the Command Floor HUD footer (`FAULT TELEMETRY: 250ms lat • 5% loss • 42 fwd • 3 drop`) or via `curl http://127.0.0.1:8000/fault` to inspect latency, loss rate, packets forwarded, and packets dropped during a demo.
- **Zero Distraction When Clean:** When no fault is active, both the badge and telemetry line remain hidden (`display: none`), preserving the clean default spatial aesthetic.
- **Local Diagnostic Endpoint:** `operator.js` polls `GET /fault`, which resolves the active degradation state from:
  1. Dynamic server state set via local `POST /fault`
  2. Automatic local state file `data/fault_status.json` (synced automatically by `FaultProxy`)
  3. `NIMBUS_FAULT_PROFILE` environment variable fallback
  4. Default baseline (`healthy` / inactive)
