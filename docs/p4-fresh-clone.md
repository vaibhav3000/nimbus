# Nimbus P4 Fresh Clone & Quick Start Guide

This guide describes how to clone, install, run, and test Nimbus Person 4 (P4) deliverables on a clean machine (Windows, macOS, or Linux).

---

## 1. Prerequisites

- **Python 3.10+** (tested on Python 3.10, 3.11, 3.12, 3.14)
- **Git**
- Optional: **Terraform 1.5+** (only needed if validating `infra/terraform/`)

---

## 2. Clone and Setup Environment

### Clone Repository
```bash
git clone https://github.com/vaibhav3000/nimbus.git
cd nimbus
```

### Create and Activate Virtual Environment

**On Windows (PowerShell):**
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

**On Linux / macOS:**
```bash
python3 -m venv .venv
source .venv/bin/activate
```

### Install Dependencies
```bash
pip install -r requirements.txt
```
*(For complete development and testing tools: `pip install pytest jsonschema requests`)*

---

## 3. Run the Nimbus Cloud Server

Start the Nimbus cloud HTTP API and static web server on default port `8000`:

```bash
# Enable demo fault badge HUD on operator console:
export NIMBUS_DEMO_FAULTS=1   # Linux/macOS
# or: $env:NIMBUS_DEMO_FAULTS="1"   # Windows PowerShell

python server.py
```
The server will log:
```
INFO: Nimbus server listening on http://127.0.0.1:8000
```

---

## 4. Open the Web Applications

Once the server is running, open your browser:

1. **Hospital Requester App**:
   Navigate to: **[`http://127.0.0.1:8000/`](http://127.0.0.1:8000/)** (or `http://127.0.0.1:8000/index.html`)
   - Submit natural-language task requests (e.g. *"Deliver paediatric pump to bed 2B-12"*).
   - Answer clarifying questions interactively.
   - Track task progress, lease version updates, and delivery proofs in real time.

2. **Operator Console**:
   Navigate to: **[`http://127.0.0.1:8000/operator.html`](http://127.0.0.1:8000/operator.html)**
   - View the hospital floor spatial grid HUD.
   - Monitor live robot positions and battery states.
   - Inspect active network fault indicators and link degradation telemetry.
   - Issue **Pause** or **Cancel** commands (cancelling automatically issues a stop lease to robots).

---

## 5. Run the P4 Test Suite

In a separate terminal (with virtual environment active), run all P4 verification tests:

```bash
python -m pytest tests/test_api.py tests/test_fault_harness.py tests/test_mqtt_transport.py tests/test_cost_backend.py -v
```

Expected result:
```
============================= 62 passed in ~25s ==============================
```

To run only the cost formula tests:
```bash
python -m pytest tests/test_cost_backend.py -v
```

---

## 6. Optional: Run the Network Fault Harness

Test edge-to-cloud communication under simulated degraded conditions:

### Shielded Room / Lift (250ms latency, 80ms jitter, 5% loss)
```bash
python scripts/fault_harness.py --listen 8001 --target 8000 --profile shielded_room
```

In a third terminal, run the edge simulation through the proxy:
```bash
python scripts/edge_sim.py --api http://127.0.0.1:8001 --cluster east-wing --complete
```
Notice the operator console at `http://127.0.0.1:8000/operator.html` immediately updates with a steel/amber fault badge and live degradation telemetry.

---

## 7. Validate Terraform Infrastructure (Optional)

```bash
cd infra/terraform
terraform init -backend=false
terraform validate
```
Expected output:
```
Success! The configuration is valid.
```
