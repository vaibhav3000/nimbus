# Nimbus Person 2 (Edge Tier) Evaluation Deliverables & Results

This directory (`P2_Results/`) contains the complete set of evaluation results, benchmark datasets, and empirical evidence required from **Person 2 (Edge Tier & Local Autonomy)** for the Nimbus system evaluation.

---

## 1. Summary of Deliverables & Verification Status

| Requirement / Experiment | Rubric File | Target SLA / Metric | Measured Result | Status |
| :--- | :--- | :--- | :--- | :---: |
| **E1: Edge SLM Latency & Model Bench**<br>*(FR-3, NFR-1 edge)* | [`e1_models.csv`](file:///C:/Users/aksha/Introduction%20to%20CLoud%20Computing/Nimbus-Edge/P2_Results/e1_models.csv) | • p95 latency $\le 10.0$ s<br>• Valid actions $\ge 95\%$<br>• Pin one model | • p50: **0.377 s**, p95: **0.495 s**<br>• Valid actions: **100.0%**<br>• Pinned: `qwen2.5:1.5b`<br>• Hardware: Intel Core Ultra 7 155H | **PASS** |
| **E2: Edge Offline Decision Quality**<br>*(FR-4)* | [`e2_situations.json`](file:///C:/Users/aksha/Introduction%20to%20CLoud%20Computing/Nimbus-Edge/P2_Results/e2_situations.json)<br>[`e2_edge.csv`](file:///C:/Users/aksha/Introduction%20to%20CLoud%20Computing/Nimbus-Edge/P2_Results/e2_edge.csv) | • 40 hand-labelled situations<br>• Decision accuracy $\ge 80.0\%$ | • 40 situations across 6 categories<br>• Rule Baseline: **95.0%** (38/40)<br>• Deployed Hybrid: **95.0%** (38/40)<br>• Zero-hallucination rate: **100%** | **PASS** |
| **E3: Log Excerpts Verification**<br>*(FR-9, FR-14, FR-15, FR-16)* | [`e3_log_excerpts.md`](file:///C:/Users/aksha/Introduction%20to%20CLoud%20Computing/Nimbus-Edge/P2_Results/e3_log_excerpts.md) | • 0 invalid leases<br>• Fallback after 2 failed proposals<br>• Urgent before routine in T8<br>• Re-plan time $\le 30$ s | • 0 invalid leases issued across all 240 runs<br>• Fallback cuts over to classical rules<br>• Urgent preempts routine at head of queue<br>• Re-plan time: **2.58 s** ($\le 30$ s SLA) | **PASS** |
| **E4: Network Trace Replay**<br>*(FR-6, NFR-2 edge side)* | [`e4.csv`](file:///C:/Users/aksha/Introduction%20to%20CLoud%20Computing/Nimbus-Edge/P2_Results/e4.csv)<br>[`netem.sh`](file:///C:/Users/aksha/Introduction%20to%20CLoud%20Computing/Nimbus-Edge/P2_Results/netem.sh)<br>[`plots/e4_recovery_plot.svg`](file:///C:/Users/aksha/Introduction%20to%20CLoud%20Computing/Nimbus-Edge/P2_Results/plots/e4_recovery_plot.svg) | • Replay traces on T2, T4, T5, T7<br>• Plot of success & recovery (C1 vs C2) | • C1 maintains **100% success** & **<2.5s recovery**<br>• C2 recovery spikes to **35.0s** under outage<br>• Linux tc/netem script included | **PASS** |
| **E5: Edge Failures & Partition Tolerance**<br>*(FR-6, FR-7, FR-10, FR-12, NFR-2)* | [`e5_edge.csv`](file:///C:/Users/aksha/Introduction%20to%20CLoud%20Computing/Nimbus-Edge/P2_Results/e5_edge.csv) | • 10-min outage: 0 lost results<br>• Crash takeover $\le 30$ s<br>• 0 duplicate physical actions<br>• Reconnect reconcile $\le 1$ min | • Lost results: **0** (store-and-forward)<br>• Takeover: **2.40 s** (0 duplicates)<br>• Reconnect: **12.30 s**<br>• Watchdog: **0.05 s** to safe bay | **PASS** |
| **C2 Flag (Cloud-Only Baseline)** | In `EdgeSupervisor(system_mode="C2")` | Local reasoning bypassed; obstacles/faults relayed to Cloud | Verified via unit and regression suite (`edge/tests/test_c2_baseline.py`) | **PASS** |
| **Shared Duty: PRD v6** | [`PRD_v6.md`](file:///C:/Users/aksha/Introduction%20to%20CLoud%20Computing/Nimbus-Edge/P2_Results/PRD_v6.md) | • Add FR-1, 2, 9, 14, 15, 16 to Table 4<br>• Settle E4/E5 MVP status<br>• Align FR-22 with E3<br>• Version bump to v6 | Updated PRD v6.0 formally mapping all 29 requirements with E4/E5 as core MVP deliverables. | **PASS** |

---

## 2. File Index & Details

### 1. `e1_models.csv`
* **File:** [`e1_models.csv`](file:///C:/Users/aksha/Introduction%20to%20CLoud%20Computing/Nimbus-Edge/P2_Results/e1_models.csv)
* **Covers:** FR-3 (Edge tactical autonomy), NFR-1 (Edge p95 latency $\le 10$ s, $\le 150$ tokens).
* **Summary:** Compares `qwen2.5:0.5b`, `qwen2.5:1.5b` (pinned), and `qwen2.5:3b` with hardware specification on Intel Core Ultra 7 155H.

### 2. `e2_situations.json` & `e2_edge.csv`
* **Files:** [`e2_situations.json`](file:///C:/Users/aksha/Introduction%20to%20CLoud%20Computing/Nimbus-Edge/P2_Results/e2_situations.json), [`e2_edge.csv`](file:///C:/Users/aksha/Introduction%20to%20CLoud%20Computing/Nimbus-Edge/P2_Results/e2_edge.csv)
* **Covers:** FR-4 (Dynamic hospital tactical scenarios), NFR-5 (Synthetic data only).
* **Summary:** 40 curated hospital situations across 6 clinical categories. Shows 95.0% accuracy on deployed hybrid edge architecture (exceeding $\ge 80.0\%$ target).

### 3. `e4.csv`, `netem.sh`, & `plots/e4_recovery_plot.svg`
* **Files:** [`e4.csv`](file:///C:/Users/aksha/Introduction%20to%20CLoud%20Computing/Nimbus-Edge/P2_Results/e4.csv), [`netem.sh`](file:///C:/Users/aksha/Introduction%20to%20CLoud%20Computing/Nimbus-Edge/P2_Results/netem.sh), [`plots/e4_recovery_plot.svg`](file:///C:/Users/aksha/Introduction%20to%20CLoud%20Computing/Nimbus-Edge/P2_Results/plots/e4_recovery_plot.svg)
* **Covers:** FR-6, NFR-2 (Edge behavior under degraded WAN).
* **Summary:** Compares Tasks T2, T4, T5, and T7 across Healthy, Mild WAN, Elevator Shaft, and Basement Outage traces. Proves C1 edge autonomy isolates robots from WAN stalls.

### 4. `e5_edge.csv`
* **File:** [`e5_edge.csv`](file:///C:/Users/aksha/Introduction%20to%20CLoud%20Computing/Nimbus-Edge/P2_Results/e5_edge.csv)
* **Covers:** FR-6 (Safe parking & Watchdog), FR-7 (Node failover), FR-10 (Store-and-forward outbox), FR-12 (Reconnection protocol), NFR-2 (Normal operation resumption).
* **Summary:** Documents 0 lost results, 2.4s failover takeover (0 duplicate steps), 12.3s lease reconciliation, and safe parking outside doors/lifts.

### 5. `e3_log_excerpts.md`
* **File:** [`e3_log_excerpts.md`](file:///C:/Users/aksha/Introduction%20to%20CLoud%20Computing/Nimbus-Edge/P2_Results/e3_log_excerpts.md)
* **Covers:** FR-9, FR-14, FR-15, FR-16.
* **Summary:** Formal log proof of 0 invalid leases, proposal retry cutover, urgent preemption in Task T8, and re-plan latency under 2.58 seconds.

### 6. `PRD_v6.md`
* **File:** [`PRD_v6.md`](file:///C:/Users/aksha/Introduction%20to%20CLoud%20Computing/Nimbus-Edge/P2_Results/PRD_v6.md)
* **Covers:** Milestone 2 PRD v6.0 updates, Table 4 traceability, and formal resolution of E4/E5 as core MVP deliverables.

---

## 3. How to Reproduce & Verify

All evaluation results can be re-executed inside the container environment using these single commands:

```bash
# 1. Run all 60 Person 2 edge unit & integration tests:
python3 -m pytest edge/tests/ -v

# 2. Re-run Experiment E1 SLM Benchmarking:
python3 edge/benchmarks/e1_benchmark.py qwen2.5:1.5b

# 3. Re-run Experiment E2 Offline Decision Quality (40 scenarios):
python3 edge/benchmarks/e2_benchmark.py

# 4. Re-run Experiment E4 Network Trace Replay:
python3 scripts/run_e4.py

# 5. Audit synthetic data manifest (NFR-5 compliance):
python3 scripts/audit_data.py
```
