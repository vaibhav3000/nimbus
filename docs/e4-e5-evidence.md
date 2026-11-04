# Experiments E4 & E5 Execution Guide & Evidence Notes

This document provides exact reproduction commands and methodology details for experiments **E4** (Latency & Jitter Degradation) and **E5** (Packet Loss Degradation) specified in PRD Section 7.

---

## 1. Important Note on Metric Provenance

> **Implementation Note on Measurement Origin:**
> All recovery timestamps, packet drop counts, and connection failure metrics reported during local evaluations and test runs are produced directly by the userspace pure-Python **`FaultProxy`** (`scripts/fault_harness.py`), **not by a live Linux kernel `tc`/`netem` run**.
>
> - **Why Userspace Proxy**: Provides cross-platform testability (Windows, macOS, Linux) without requiring `sudo` privileges or specialized kernel modules.
> - **Linux `tc`/`netem` Alternative**: The harness supports generating and applying real Linux `tc qdisc` rules via `--tc-script` and `--tc-apply` for hardware deployments (e.g. Raspberry Pi testbeds). When testing locally without root access, the userspace proxy is the active mechanism.

---

## 2. Experiment E4: Latency & Jitter Degradation

### Goal
Evaluate system responsiveness, task handover, timeline tracking, and lease renewal under WAN latency and jitter without triggering spurious lease expirations.

### Exact Reproduction Commands

#### Method A: Integrated In-Process Proxy (Single-Command)
```bash
# Terminal 1: Start Nimbus cloud server
python server.py

# Terminal 2: Run edge simulation through mild WAN conditions (60ms latency, 15ms jitter)
python scripts/edge_sim.py --fault-profile mild_wan --cluster east-wing --complete
```

#### Method B: Severe Degradation (Elevator / Shielded Room)
```bash
# Run edge simulation through shielded room conditions (250ms latency, 80ms jitter)
python scripts/edge_sim.py --fault-profile shielded_room --cluster east-wing --complete
```

#### Method C: Dynamic 10 Hz Campus Trace Playback
```bash
# Terminal 1: Start cloud server
python server.py

# Terminal 2: Start standalone FaultProxy on port 8001 replaying elevator trace
python scripts/fault_harness.py --listen 8001 --target 8000 --trace traces/iisc_lift.csv

# Terminal 3: Run edge simulation through degraded proxy port
python scripts/edge_sim.py --api http://127.0.0.1:8001 --cluster east-wing --complete
```

### Verification
- Check task completion timeline:
  ```bash
  curl http://127.0.0.1:8000/tasks/<task_id>/timeline
  ```
- Verify lease handovers and check-ins complete successfully with timestamps reflecting injected delays.

---

## 3. Experiment E5: Packet Loss Degradation

### Goal
Measure retry resilience, timeout handling, and message delivery when packet loss occurs between the edge cluster and the cloud.

### Userspace Drop Semantics
To prevent corrupting mid-stream HTTP framing or JSON payloads at the byte level, `FaultProxy` applies connection-level drop semantics evaluated once upon connection ingress. When a drop occurs, the TCP connection is immediately aborted (`close()`), forcing the edge client's transport layer to exercise retries and outbox buffering.

### Exact Reproduction Commands

#### Method A: Preset Severe Loss Profile (15% Loss Rate)
```bash
# Terminal 1: Start cloud server
python server.py

# Terminal 2: Run edge simulation with severe packet loss
python scripts/edge_sim.py --fault-profile severe --cluster east-wing --complete
```

#### Method B: Custom Loss Rate (e.g. 10% Packet Loss)
```bash
# Terminal 1: Start cloud server
python server.py

# Terminal 2: Start standalone FaultProxy with 10% packet drop probability
python scripts/fault_harness.py --listen 8001 --target 8000 --loss 0.10

# Terminal 3: Run edge simulation targeting proxy port
python scripts/edge_sim.py --api http://127.0.0.1:8001 --cluster east-wing --complete
```

### Verification
- Check proxy drop counters via the local diagnostic API:
  ```bash
  curl http://127.0.0.1:8000/fault
  ```
- Confirm `packets_dropped` and `connections_rejected` are non-zero while tasks still complete via edge retries.

---

## 4. Automated Test Verification

Run the automated regression tests covering E4 and E5 fault injection:
```bash
python -m pytest tests/test_fault_harness.py -q
```
All tests verify proxy degradation, trace replay, and outbox recovery under simulated link impairments.
