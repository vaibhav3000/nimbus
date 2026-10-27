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

## 7. Mapping to PRD Experiments

- **Experiment E4 (Network degradation on T2, T4, T5, T7):**
  Use `--profile mild_wan` and `--trace traces/iisc_lift.csv` to measure task completion times and recovery latency as link quality degrades.
- **Experiment E5 (Outage resilience & recovery time, NFR-2):**
  Use `--profile basement_outage` or `--trace traces/iisc_basement.csv` to cut connectivity for up to 10 minutes. Verify that edge supervisors park robots safely (FR-6) and upload all pending results upon reconnection with zero data loss (FR-10).
- **Experiment E6 (Re-ordered & repeated messages):**
  Use jitter and packet loss to induce TCP packet retransmission and out-of-order delivery, verifying that Nimbus deduplication (FR-8) prevents duplicate actions.
