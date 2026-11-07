# Nimbus Network Fault Injection Harness Semantics & Architecture

## Overview
The Nimbus Network Fault Injection Harness (`scripts/fault_harness.py`) provides simulated degradation, delays, packet loss, and full disconnection/partition on the edge-to-cloud link. It supports both local experimentation (Windows, macOS, Linux) without root/kernel modules, and hardware/container testbed emulation using Linux kernel traffic control (`tc/netem`).

---

## 1. User-Space Proxy vs. Kernel tc/netem Semantics

### User-Space Streaming Proxy Mode
- **Drop Semantics**: In user-space streaming proxy mode, configured `loss_rate` operates at the **TCP connection level**. When a drop is triggered (`profile.should_drop()`), the proxy terminates/aborts the socket connection immediately (`_safe_close`).
  > **Important**: This is a **connection abort**, NOT mid-stream IP packet drop or byte-level truncation. Applications experience connection resets/refusals or dropped socket streams.
- **Latency & Jitter**: Artificial delay is computed non-negatively (`delay_ms = max(0.0, latency_ms + uniform(-jitter, +jitter)) / 1000.0`). The delay is applied per connection stream burst rather than accumulating unbounded latency on every 64KB chunk.
- **Partition / Disconnection**: Drops all incoming TCP connection attempts (`connections_rejected`).

### Kernel-Level `tc/netem` Mode (`--tc-script`)
- Generates standard Linux `iproute2 / tc` queueing discipline rules attached to network interfaces (e.g. `eth0` or `veth`):
  ```bash
  sudo tc qdisc add dev eth0 root netem delay 200.0ms 50.0ms distribution normal loss 5.00%
  ```
- **True Kernel Packet Loss**: Injects actual mid-stream IP packet drops in the kernel network stack, triggering TCP retransmissions, window scaling, and congestion control regressions.

---

## 2. Fault Metadata and Event Schema

Every fault injection event and profile status contains:
- `fault_id`: Unique identifier (e.g., `flt-a1b2c3d4`) identifying the specific fault activation.
- `fault_class`: Explicit classification of the degradation mode:
  - `connection_abort`: User-space proxy upfront TCP connection termination.
  - `latency`: Artificial transmission delay and jitter.
  - `partition`: Total network disconnection (100% loss / unreachable).
  - `healthy`: Normal operational state.
- `generation`: Monotonically increasing version counter preventing race conditions where late status updates or background threads revive a previously cleared/reset fault state.
- `timestamp`: Unix epoch float of when the profile was applied.
- `connections_aborted`: Count of connections terminated due to loss simulation.

---

## 3. Status Synchronization and Security

- When running against a cloud server (`server.py`), the proxy synchronizes state via `POST /fault`.
- Because `server.py` enforces fail-closed authentication (C1), fault status updates transmit `Authorization: Bearer <token>` using `NIMBUS_ADMIN_TOKEN`, `NIMBUS_FAULT_TOKEN`, or `NIMBUS_EDGE_TOKEN`.
- On shutdown, `_clear_fault_status()` increments `generation`, deletes local `data/fault_status.json`, and sends a reset payload to the server.
