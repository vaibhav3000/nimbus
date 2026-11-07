# Nimbus Experiment Verification and Provenance Status (N1–N10 & C1–C9)

**Document Purpose:** Complete audit and classification of Project Nimbus evaluation experiments N1 through N10 (Network & System) and C1 through C9 (Cloud Orchestrator & Bedrock) in accordance with Red-Team Addendum Section 7.  
**Baseline Git Commit:** `29f37e7540fe7d130c845d0139dff77d8f827e78`  
**Shared Execution Module:** `scripts/p4_shared.py` (restored and verified).

---

## 1. Network & System Experiments Audit (N1–N10)

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

## 2. Cloud Experiments Provenance Audit (C1–C9)

*Note on Provenance & Commit Reconciliation:* Historical commit logs and raw evaluation artifacts in `results/final/` confirm that C1, C2, C3, C6, C7, C8, and C9 were evaluated against **real Amazon Bedrock (`apac.amazon.nova-lite-v1:0`)**, with `C1_deployed` evaluated against the live deployed AWS cloud in `ap-south-1`. Companion `_standin` and `_local` suites provide offline reproducible verification using deterministic rule-based stand-in models (`mock_llm`). C4 and C5 represent deterministic software measurements (`measured`).

*Disambiguation of Result Commits vs. Execution Base Commits:* `env.json` captures the baseline repository commit active when a test script began execution, whereas the Git commit that recorded and committed the verified experiment outputs into the repository is the **Result Commit**.
- **C1**: Result Commit `4c97ce3` (Execution Base in env.json: `485d898`)
- **C2**: Result Commit `a76c160` (Execution Base in env.json: `485d898`)
- **C3**: Result Commit `1c00f6f` (Execution Base in env.json: `485d898`)
- **C6**: Result Commit `485d898` (Execution Base in env.json: `9a80a4e`)
- **C7**: Result Commit `6c89d37` (Execution Base in env.json: `65892f6`)
- **C8**: Result Commit `65892f6` (Execution Base in env.json: `0489b22`)
- **C9**: Result Commit `0489b22` (Execution Base in env.json: `4c97ce3-dirty`)

| ID | Experiment Title | PRD Goal | Provenance / Category | Target / Environment | Model Used | Result Commit | Evidence Path |
| :--- | :--- | :--- | :--- | :--- | :--- | :---: | :--- |
| **C1** | Request Understanding | FR-13 | **Real Bedrock Execution** (`real_bedrock`) | Local server (`macOS arm64`) | `apac.amazon.nova-lite-v1:0` | `4c97ce3` | `results/final/C1/` |
| **C1_deployed** | Request Understanding (AWS) | FR-13 | **Real Bedrock Execution** (`real_bedrock`) | Deployed AWS API Gateway (`ap-south-1`) | `apac.amazon.nova-lite-v1:0` | `071bbc6` | `results/final/C1_deployed/` |
| **C1_standin** | Request Understanding (Offline) | FR-13 | **Local Simulation** (`mock_llm`) | Local server (`Linux x86_64`) | `rule-based-stand-in` | `071bbc6` | `results/final/C1_standin/` |
| **C2** | Rules & Shared Map | FR-17 | **Real Bedrock Execution** (`real_bedrock`) | Local server (`macOS arm64`) | `apac.amazon.nova-lite-v1:0` | `a76c160` | `results/final/C2/` |
| **C2_standin** | Rules & Shared Map (Offline) | FR-17 | **Local Simulation** (`mock_llm`) | Local server (`Linux x86_64`) | `rule-based-stand-in` | `071bbc6` | `results/final/C2_standin/` |
| **C3** | Memory & Habits | FR-19 | **Real Bedrock Execution** (`real_bedrock`) | Local server (`macOS arm64`) | `apac.amazon.nova-lite-v1:0` | `1c00f6f` | `results/final/C3/` |
| **C3_standin** | Memory & Habits (Offline) | FR-19 | **Local Simulation** (`mock_llm`) | Local server (`Linux x86_64`) | `rule-based-stand-in` | `071bbc6` | `results/final/C3_standin/` |
| **C4** | Rule Checker Under Attack | FR-15 | **Measured Verification** (`measured`) | In-process cloud (`Linux x86_64`) | Adversarial attacker | `071bbc6` | `results/final/C4/` |
| **C5** | Priority Scheduling & Inversion | FR-14, T8 | **Measured Verification** (`measured`) | In-process simulated clock | EDF scheduler | `071bbc6` | `results/final/C5/` |
| **C6** | Re-plan & Hand-over Latency | FR-16, T5 | **Real Bedrock Execution** (`real_bedrock`) | Local server (`macOS arm64`) | `apac.amazon.nova-lite-v1:0` | `485d898` | `results/final/C6/` |
| **C6_local** | Re-plan Latency (Offline) | FR-16, T5 | **Local Simulation** (`mock_llm`) | Local server (`Linux x86_64`) | `rule-based-stand-in` | `071bbc6` | `results/final/C6_local/` |
| **C7** | Cloud Model Outage Fallback | FR-18 | **Real Bedrock Execution** (`real_bedrock`) | Local server (`macOS arm64`) | `apac.amazon.nova-lite-v1:0` | `6c89d37` | `results/final/C7/` |
| **C7_local** | Model Outage Fallback (Offline) | FR-18 | **Local Simulation** (`mock_llm`) | Local server (`Linux x86_64`) | `rule-based-stand-in` | `071bbc6` | `results/final/C7_local/` |
| **C8** | Latency Under Parallel Load | NFR-1 | **Real Bedrock Execution** (`real_bedrock`) | Local server (`macOS arm64`) | `apac.amazon.nova-lite-v1:0` | `65892f6` | `results/final/C8/` |
| **C8_local** | Parallel Load (Offline) | NFR-1 | **Local Simulation** (`mock_llm`) | Local server (`Linux x86_64`) | `rule-based-stand-in` | `071bbc6` | `results/final/C8_local/` |
| **C9** | Cloud Model Calls Per Task | NFR-4 | **Real Bedrock Execution** (`real_bedrock`) | Local server (`macOS arm64`) | `apac.amazon.nova-lite-v1:0` | `0489b22` | `results/final/C9/` |
| **C9_standin** | Model Calls Per Task (Offline) | NFR-4 | **Local Simulation** (`mock_llm`) | Local server (`Linux x86_64`) | `rule-based-stand-in` | `071bbc6` | `results/final/C9_standin/` |

---

## 3. Detailed Experiment Audit Findings (N1–N10)

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
- **Finding:** Injected 500 duplicate, out-of-order, and stale lease messages across 10 seeded runs against a live local HTTP server testbed (`server.make_server`). Verified that in-flight reservation and persistent deduplication prevented all duplicate task state transitions (zero duplicate steps executed, 100% pass bar).

### N5 — Selective Sharing
- **Status:** `Fixed` (`simulated`).
- **Finding:** Evaluated 300 corridor blockage and quarantine events across `east-wing` and `west-wing`. Expected affected clusters were computed independently from raw `pack.topology_doc` spatial node boundaries and active lease reservations (completely independent of `MapState.affected_clusters`). Measured 100.0% recall (0 false negatives) and 100.0% precision, ensuring zero cross-wing telemetry leakage.

### N6 — Audit Replay & Determinism
- **Status:** `Fixed` (`simulated`).
- **Finding:** Replayed 10 seeded execution logs using independent Python subprocesses with distinct `PYTHONHASHSEED` environment variables. Generated bitwise identical timeline reconstructions (`e6_seed_diff.txt` is 0 bytes), verifying deterministic reproducibility.

### N7 — Security and Privacy Audit
- **Status:** `Fixed` (`measured_real_system`).
- **Finding:** Executed 10 automated, dynamic audit checks against actual repo source files, contracts, and Terraform configurations (zero hard-coded True checks):
  - 0 plaintext credentials found across full Git history and tracked files (`audit_iam.secret_findings`).
  - Scoped IAM policies confirmed for DynamoDB, SQS, S3, Secrets Manager, and Bedrock.
  - 100% of evaluation datasets confirmed synthetic (no PHI/PII in manifest or traces).
  - Camera payloads dynamically confirmed restricted to `OUTPUT_READY` payloads.

### N8 — Fleet Scalability
- **Status:** `Fixed` (`modelled`).
- **Finding:** Modelled 3 to 15 robots under time-varying MIMIC-IV emergency department arrival patterns. 10-robot latency ratio was $1.084\times \le 1.50\times$, and 15-robot latency ratio was $1.125\times \le 1.50\times$.

### N9 — Cost Accounting Breakdown
- **Status:** `Fixed` (`modelled`).
- **Finding:** Calculated per-task cost under PRD pricing formula using Bedrock Nova-Lite rates (\$0.06 / 1M input tokens, \$0.24 / 1M output tokens) and Fargate rates ($0.04048/vCPU-hr, $0.004445/GB-hr). Clarified that this reflects mathematical unit cost modeling, not live AWS Cost Explorer billing. C1 model call ratio was 24.4% of C2 ($\le 50.0\%$). C1 average cost per task was \$0.000544.

### N10 — Live Operator Console & Requester Validation
- **Status:** `Fixed` (`measured_real_system`).
- **Finding:** Validated against live AWS deployment (`qrgdidp9te.execute-api.ap-south-1.amazonaws.com`).
  - Recorded 1080p high-resolution screen captures:
    - `results/final/N10/demo_completed_flow.webm` (SC-1 end-to-end delivery)
    - `results/final/N10/demo_cancel_flow.webm` (real-time operator cancellation)
  - Operator cancel stop latency measured across 40 live operations: mean 0.293 s (p95 = 0.328 s $\le 5.0$ s).
  - Note on scope: Latency measures cloud API cancellation and stop lease publication (`Request created` -> `Stop lease published` -> `Console ACK`); physical robot deceleration to complete halt is verified via video/simulation playback, not on-robot IMU logging.

---

## 4. Detailed Cloud Experiment Audit Findings (C1–C9)

### C1 — Plain-Language Request Understanding
- **Status:** `Verified` (`real_bedrock`).
- **Commits:** Result Commit `4c97ce3` (Execution Base in env.json: `485d898`).
- **Provenance:** 300 clinical requests from `data/hospital_sim6/requests_test.jsonl` parsed via Bedrock Nova-Lite (`apac.amazon.nova-lite-v1:0`). Field accuracy: 98.1% (95% CI: [97.0%, 98.9%]), clarification recall: 75.0%, precision: 98.4%.
- **Deployed Cloud Verification:** `C1_deployed` confirmed 94.0% field accuracy and 72.6% clarification recall over live AWS API Gateway and ECS Fargate (`results/final/C1_deployed/`, commit `071bbc6` / `81f0b93-dirty`).
- **Offline Stand-in:** `C1_standin` reproduces pipeline with deterministic rule-based stand-in (`results/final/C1_standin/`).

### C2 — Natural Language Rules & Shared Map Updates
- **Status:** `Verified` (`real_bedrock`).
- **Commits:** Result Commit `a76c160` (Execution Base in env.json: `485d898`).
- **Provenance:** Evaluated 10 natural language rules and 20 dynamic map updates. Nova Lite correctly interpreted 9/10 rules (1 complex phrasing edge-case honestly recorded as fail on strict 10/10 bar); all map updates reflected within 5 minutes (p95 = 1.60 s).

### C3 — Memory & Clinician Habit Disambiguation
- **Status:** `Verified` (`real_bedrock`).
- **Commits:** Result Commit `1c00f6f` (Execution Base in env.json: `485d898`).
- **Provenance:** 20 context-dependent requests tested with memory enabled vs disabled. 10/10 habit cases disambiguated without unnecessary follow-up questions; 0 habit overrides when explicit destination specified.

### C4 — Rule Checker Adversarial Defense
- **Status:** `Verified` (`measured`).
- **Commits:** Result Commit `071bbc6` (Execution Base in env.json: `81f0b93-dirty`).
- **Provenance:** 180 invalid lease proposals generated by adversary simulator (unknown cluster, wrong robot, cross-zone assignment, closed zone, unknown destination). 100% (180/180) vetoed with expected rule cited; 0/20 valid controls wrongly vetoed; rule-based fallback triggered after 2 failed model allocations.

### C5 — Priority Queue Scheduling & Inversion Prevention
- **Status:** `Verified` (`measured`).
- **Commits:** Result Commit `071bbc6` (Execution Base in env.json: `81f0b93-dirty`).
- **Provenance:** 113,518 tasks evaluated across 10 random seeds and 6 load levels on simulated hospital clock. Nimbus EDF scheduler exhibited exactly 0 priority inversions (compared to 25,009 inversions in FIFO baseline), maintaining 0.0 urgent task deadline miss ratio at load factor $\rho = 1.3$.

### C6 — Re-planning & Hand-over Latency
- **Status:** `Verified` (`real_bedrock`).
- **Commits:** Result Commit `485d898` (Execution Base in env.json: `9a80a4e`).
- **Provenance:** 32 unexpected edge events (corridor blockage, robot fault, handover request). Decision p95 = 1.58 s (p50 = 0.84 s, model latency p50 = 876 ms), well within 30-second requirement. 18/18 revised lease versions successfully avoided closed zones and failed robots.

### C7 — Cloud Model Outage Fallback
- **Status:** `Verified` (`real_bedrock`).
- **Commits:** Result Commit `6c89d37` (Execution Base in env.json: `65892f6`).
- **Provenance:** Injected 600-second Bedrock API outage mid-run. 14 requests arrived during outage: 5 leased via rule fallback, 9 queued, 0 model crashes, 100% issued with reduced-autonomy notice. 14/14 tasks leased prior to outage completed without disruption.

### C8 — Latency Under Parallel Load
- **Status:** `Verified` (`real_bedrock`).
- **Commits:** Result Commit `65892f6` (Execution Base in env.json: `0489b22`).
- **Provenance:** Evaluated under closed-loop concurrency (up to 64 parallel client workers) and open-loop Poisson traffic. Worst p95 latency = 2.13 s with 0 HTTP errors. Fitted queue capacity = ~44.9 requests/s.

### C9 — Cloud Model Calls per Task (C1 vs C2 vs C3)
- **Status:** `Verified` (`real_bedrock`).
- **Commits:** Result Commit `0489b22` (Execution Base in env.json: `4c97ce3-dirty`).
- **Provenance:** Measured model call consumption across T1–T5. Leased edge autonomy (C1) consumed 1.18 model calls/task compared to 5.27 calls/task in cloud-only relay (C2), achieving a call ratio of 22.4% (well under the 50.0% NFR-4 threshold).

