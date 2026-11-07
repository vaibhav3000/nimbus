# P4 Experiment Verification and Provenance Status (N1–N10)

**Document Purpose:** Complete audit and classification of Project Nimbus evaluation experiments N1 through N10 in accordance with Red-Team Addendum Section 7.  
**Baseline Git Commit:** `29f37e7540fe7d130c845d0139dff77d8f827e78`  
**Shared Execution Module:** `scripts/p4_shared.py` (restored and verified).

---

## 1. Master Experiment Audit and Evidence Classification

| ID | Experiment Name | Requirement Tested | Evaluated Data Source | Status / Category | Reproducible Runner Command | Evidence Path |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **N1** | IISc Campus Field Traces | E4, FR-6 | *None* (synthetic sample only) | **Not run** (Blocked on physical campus access) | *N/A* | `traces/` |
| **N2** | Poisson Outage Modeling | Outage distribution & survival | Synthetic walk traces (`traces/`) | **Fixed** (`modelled`) | `python results/final/N2/run_n2.py` | `results/final/N2/` |
| **N3** | Network Trace Replay | E4, FR-6, NFR-2 | Network degradation emulator | **Fixed** (`simulated`) | `python results/final/N3/run_n3.py` | `results/final/N3/` |
| **N4** | Message Robustness | E6, FR-8 | Injected duplicate/stale messages | **Fixed** (`simulated`) | `python results/final/N4/run_n4.py` | `results/final/N4/` |
| **N5** | Selective Sharing | FR-11 | Geographic cluster boundary matrix | **Fixed** (`simulated`) | `python results/final/N5/run_n5.py` | `results/final/N5/` |
| **N6** | Audit Replay Determinism | FR-20, NFR-6 | Reconstructed decision logs | **Fixed** (`simulated`) | `python results/final/N6/run_n6.py` | `results/final/N6/` |
| **N7** | Security & Privacy Audit | NFR-5 | Live Terraform, Git & datasets | **Fixed** (`measured_real_system`)| `python results/final/N7/run_n7.py` | `results/final/N7/` |
| **N8** | Fleet Scalability | E7, NFR-3 | MIMIC-IV shift arrival queue model| **Fixed** (`modelled`) | `python results/final/N8/run_n8.py` | `results/final/N8/` |
| **N9** | Cost Accounting Breakdown | NFR-4 | Bedrock Nova-Lite pricing model | **Fixed** (`modelled`) | `python results/final/N9/run_n9.py` | `results/final/N9/` |
| **N10**| Operator Console Validation | FR-21, FR-22 | Live AWS deployment (`ap-south-1`) | **Fixed** (`measured_real_system`)| `python results/final/N10/run_n10.py` | `results/final/N10/` |

---

## 2. Detailed Experiment Audit Findings

### N1 — IISc Campus Field Traces
- **Status:** `Not run`.
- **Finding:** No genuine continuous 48-hour physical Wi-Fi trace collection on the IISc campus was possible within the test environment.
- **Remediation:** Any claims of physical Raspberry Pi field measurements were formally withdrawn. Traces in `traces/` (`synthetic_walk.csv`, `corridor_drop.csv`) are strictly labeled as synthetic benchmarks.

### N2 — Poisson Outage Modeling
- **Status:** `Fixed` (`modelled`).
- **Finding:** Fitted outage inter-arrivals from synthetic traces using MLE. Kolmogorov-Smirnov test confirmed Poisson goodness-of-fit ($p = 0.8527 \ge 0.05$). Outage duration survival curves selected Weibull distribution via Akaike Information Criterion ($\text{AIC} = 325.4$ vs Exponential $374.2$).
- **Provenance:** Correctly labeled as mathematical Monte Carlo simulation (`modelled`).

### N3 — Network Trace Replay
- **Status:** `Fixed` (`simulated`).
- **Finding:** Evaluated 720 runs across configurations C1, C2, C3 over 6 network degradation profiles (loss rates up to 25%, RTTs up to 500ms). C1 achieved 100.0% completion and recovered in 1.31 s (p95 = 3.40 s $\le 60$ s).
- **Provenance:** Execution executed via synthetic trace replayer and simulated network proxy (`simulated`).

### N4 — Message Robustness & Deduplication
- **Status:** `Fixed` (`simulated`).
- **Finding:** Injected 500 duplicate, out-of-order, and replayed messages across 10 seeded runs. Verified that in-flight reservation and persistent deduplication prevented all duplicate task state transitions ($0.00$ duplicate executions, 100% pass bar).

### N5 — Selective Sharing
- **Status:** `Fixed` (`simulated`).
- **Finding:** Evaluated 300 corridor blockage and quarantine events across `east-wing` and `west-wing`. Measured 100.0% recall (0 false negatives) and 100.0% precision, ensuring zero cross-wing telemetry leakage.

### N6 — Audit Replay & Determinism
- **Status:** `Fixed` (`simulated`).
- **Finding:** Replayed 10 seeded execution logs through independent replay engine. Generated bitwise identical timeline reconstructions (`e6_seed_diff.txt` is 0 bytes), verifying deterministic reproducibility.

### N7 — Security and Privacy Audit
- **Status:** `Fixed` (`measured_real_system`).
- **Finding:** Executed automated audit script against actual repo files and Terraform configurations:
  - 0 plaintext credentials found across full Git history.
  - Scoped IAM policies confirmed for DynamoDB, SQS, S3, and Bedrock.
  - 100% of evaluation datasets confirmed synthetic (no PHI/PII).
  - Camera payloads restricted to `OUTPUT_READY` payloads.

### N8 — Fleet Scalability
- **Status:** `Fixed` (`modelled`).
- **Finding:** Modelled 3 to 15 robots under time-varying MIMIC-IV emergency department arrival patterns. 10-robot latency ratio was $1.084\times \le 1.50\times$, and 15-robot latency ratio was $1.125\times \le 1.50\times$.

### N9 — Cost Accounting Breakdown
- **Status:** `Fixed` (`modelled`).
- **Finding:** Calculated per-task cost under PRD pricing formula using Bedrock Nova-Lite rates (\$0.06 / 1M input tokens, \$0.24 / 1M output tokens). C1 model call ratio was 24.4% of C2 ($\le 50.0\%$). C1 average cost per task was \$0.000544.

### N10 — Live Operator Console & Requester Validation
- **Status:** `Fixed` (`measured_real_system`).
- **Finding:** Validated against live AWS deployment (`qrgdidp9te.execute-api.ap-south-1.amazonaws.com`).
  - Recorded 1080p high-resolution screen captures:
    - `results/final/N10/demo_completed_flow.webm` (SC-1 end-to-end delivery)
    - `results/final/N10/demo_cancel_flow.webm` (real-time operator cancellation)
  - Operator cancel stop latency measured across 40 live operations: mean 0.293 s (p95 = 0.328 s $\le 5.0$ s).
