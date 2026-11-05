# Nimbus Person 2 (Edge Tier) Evaluation Deliverables & Results

This directory (`P2_Results/`) contains the complete set of evaluation results, benchmark datasets, and empirical evidence required from **Person 2 (Edge Tier & Local Autonomy)** for the Nimbus system evaluation.

---

## 1. Summary of Deliverables & Verification Status

| Requirement / Experiment | Rubric File | Target SLA / Metric | Measured Result | Status |
| :--- | :--- | :--- | :--- | :---: |
| **E1: Edge SLM Latency & Model Bench**<br>*(FR-3, NFR-1 edge)* | [`e1_models.csv`](file:///C:/Users/aksha/Introduction%20to%20CLoud%20Computing/Nimbus-Edge/P2_Results/e1_models.csv) | • p95 latency $\le 10.0$ s<br>• Valid actions $\ge 95\%$<br>• Pin one model | • p50: **0.377 s**, p95: **0.495 s**<br>• Valid actions: **100.0%**<br>• Pinned: `qwen2.5:1.5b` | **PASS** |
| **E2: Edge Offline Decision Quality**<br>*(FR-4)* | [`e2_situations.json`](file:///C:/Users/aksha/Introduction%20to%20CLoud%20Computing/Nimbus-Edge/P2_Results/e2_situations.json)<br>[`e2_edge.csv`](file:///C:/Users/aksha/Introduction%20to%20CLoud%20Computing/Nimbus-Edge/P2_Results/e2_edge.csv) | • 40 hand-labelled situations<br>• Decision accuracy $\ge 80.0\%$ | • 40 situations across 6 categories<br>• Rule Baseline: **95.0%** (38/40)<br>• Deployed Hybrid: **95.0%** (38/40) | **PASS** |
| **E5: Edge Failures & Partition Tolerance**<br>*(FR-6, FR-7, FR-10, FR-12, NFR-2)* | [`e5_edge.csv`](file:///C:/Users/aksha/Introduction%20to%20CLoud%20Computing/Nimbus-Edge/P2_Results/e5_edge.csv) | • 10-min outage: 0 lost results<br>• Crash takeover $\le 30$ s<br>• 0 duplicate physical actions<br>• Reconnect reconcile $\le 1$ min | • Lost results: **0** (store-and-forward)<br>• Takeover: **2.40 s** (0 duplicates)<br>• Reconnect: **12.30 s**<br>• Watchdog: **0.05 s** to safe bay | **PASS** |
| **C2 Flag (Cloud-Only Baseline)** | In `EdgeSupervisor(system_mode="C2")` | Local reasoning bypassed; obstacles/faults relayed to Cloud | Verified via unit and regression suite (`edge/tests/test_c2_baseline.py`) | **PASS** |

---

## 2. File Index & Details

### 1. `e1_models.csv`
* **File:** [`e1_models.csv`](file:///C:/Users/aksha/Introduction%20to%20CLoud%20Computing/Nimbus-Edge/P2_Results/e1_models.csv)
* **Covers:** FR-3 (Edge tactical autonomy), NFR-1 (Edge p95 latency $\le 10$ s, $\le 150$ tokens).
* **Summary Table:**
  ```csv
  model,parameter_size,quantization,tokens_per_sec,p50_latency_s,p95_latency_s,valid_actions_pct,target_compliance
  qwen2.5:0.5b,0.49B,Q4_K_M,42.5,0.220,0.380,95.0%,PASS (p95 <= 10.0s)
  qwen2.5:1.5b,1.54B,Q4_K_M,28.4,0.377,0.495,100.0%,PASS (p95 <= 10.0s)
  qwen2.5:3b,3.09B,Q4_K_M,14.8,0.720,1.150,100.0%,PASS (p95 <= 10.0s)
  ```
* **Hardware Specification:**
  * Intel(R) Core(TM) Ultra 7 155H (16 Cores, 22 Threads, 3.8 GHz Turbo), 16.0 GB System RAM, WSL2 Ubuntu 22.04 LTS
  * Ollama Engine Version 0.16.0 (`llamacpp` multi-threaded AVX2/VNNI acceleration)
* **Pinned Model:** `pinned model = qwen2.5:1.5b` (Selected for 100% strict JSON schema validity and 0.495 s p95 response time).

---

### 2. `e2_situations.json`
* **File:** [`e2_situations.json`](file:///C:/Users/aksha/Introduction%20to%20CLoud%20Computing/Nimbus-Edge/P2_Results/e2_situations.json)
* **Covers:** FR-4 (Dynamic hospital tactical scenarios), NFR-5 (Synthetic data only).
* **Content:** 40 curated and reviewed tactical situations across six clinical challenge categories:
  1. **Transient Obstacle** (10 cases): Pedestrians, food trolleys, cleaning carts $\to$ `LOCAL_WAIT`
  2. **Static Detour** (8 cases): Maintenance scaffolding, cordoned areas $\to$ `LOCAL_DETOUR`
  3. **Hardware Fault** (6 cases): Motor stall, odometry loss, sensor disconnect $\to$ `SWAP_ROBOT`
  4. **Ambiguity** (6 cases): Unspecified equipment bay or bedside $\to$ `ESCALATE_CLARIFY`
  5. **Route Closure** (6 cases): Closed fire doors, biohazard lockdowns $\to$ `ESCALATE_UNEXPECTED`
  6. **Lease Expiry** (4 cases): Watchdog expiration ($\le 30$ s remaining) $\to$ `SAFE_PARK`
* Fully cataloged in [`evaluation/manifest.json`](file:///C:/Users/aksha/Introduction%20to%20CLoud%20Computing/Nimbus-Edge/evaluation/manifest.json) and audited via `scripts/audit_data.py`.

---

### 3. `e2_edge.csv`
* **File:** [`e2_edge.csv`](file:///C:/Users/aksha/Introduction%20to%20CLoud%20Computing/Nimbus-Edge/P2_Results/e2_edge.csv)
* **Covers:** FR-4 Evaluation Results.
* **Format:** Ready for Person 3 to merge alongside `results/e2_cloud.csv`.
* **Columns:** `situation_id`, `category`, `description`, `ground_truth`, `edge_slm_action`, `edge_slm_match`, `edge_rule_action`, `edge_rule_match`, `deployed_edge_action`, `decided_by`, `deployed_edge_match`, `latency_ms`.
* **Key Results:**
  * Deterministic Rule Engine Accuracy: **95.0%** (38/40 correct)
  * Deployed Hybrid (SLM + Safety Reflex): **95.0%** (38/40 correct)
  * Requirement Target: $\ge 80.0\%$ $\to$ **PASS (EXCEEDS TARGET)**
  * Schema Hallucination Rate: **0.0%** (100% valid JSON)

---

### 4. `e5_edge.csv`
* **File:** [`e5_edge.csv`](file:///C:/Users/aksha/Introduction%20to%20CLoud%20Computing/Nimbus-Edge/P2_Results/e5_edge.csv)
* **Covers:** FR-6 (Safe parking & Watchdog), FR-7 (Node failover), FR-10 (Store-and-forward outbox), FR-12 (Reconnection protocol), NFR-2 (Normal operation resumption).
* **Summary Table:**
  ```csv
  fault_scenario,tested_requirement,recovery_time_s,lost_results,parked_positions,takeover_time_s,duplicate_physical_actions,target_sla,compliance_status
  10-min WAN Outage,FR-6 / FR-10,1.80s,0,safe-parking-bay-2,N/A,0 duplicate executions,Held messages delivered & 0 results lost,PASS
  Primary Node Kill Mid-Task,FR-7,N/A,0,N/A,2.40s,0 duplicate commands,Takeover in <= 30s with no step repeated,PASS
  WAN Reconnection & Reconciliation,FR-12,12.30s,0,All active leases reconciled,N/A,0 duplicate leases,Summary sent & reconciled in <= 1 min,PASS
  Imminent Lease Expiry Watchdog,FR-6,0.05s,0,safe-parking-bay-1 (outside corridor),N/A,0 unparked robots,In-flight cancelled & parked in safe bay,PASS
  Normal Operation Recovery,NFR-2,68.00s,0,Nominal hospital transport restored,N/A,0 lease violations,Normal operation resumes in 1-2 min,PASS
  ```
* **Underlying Test Evidence:**
  * FR-6 & FR-10: [`edge/tests/test_outbox_resilience.py`](file:///C:/Users/aksha/Introduction%20to%20CLoud%20Computing/Nimbus-Edge/edge/tests/test_outbox_resilience.py)
  * FR-7: [`edge/tests/test_failover_takeover.py`](file:///C:/Users/aksha/Introduction%20to%20CLoud%20Computing/Nimbus-Edge/edge/tests/test_failover_takeover.py)
  * FR-12: [`edge/tests/test_outbox_resilience.py`](file:///C:/Users/aksha/Introduction%20to%20CLoud%20Computing/Nimbus-Edge/edge/tests/test_outbox_resilience.py)
  * FR-6 Watchdog: [`edge/tests/test_boundary_and_parking.py`](file:///C:/Users/aksha/Introduction%20to%20CLoud%20Computing/Nimbus-Edge/edge/tests/test_boundary_and_parking.py)

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

# 4. Audit synthetic data manifest (NFR-5 compliance):
python3 scripts/audit_data.py
```
