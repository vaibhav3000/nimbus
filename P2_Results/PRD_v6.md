# Product Requirements Document (PRD v6.0)
## Nimbus: Leased Authority for Ground-Robot Fleet Navigation Across Edge and Cloud

* **Document Version:** 6.0  
* **Effective Date:** 6 November 2026  
* **Authors:** Team Nimbus (Person 1: Simulation & Safety, Person 2: Edge Tier, Person 3: Cloud Orchestration, Person 4: Infrastructure & Logging)  
* **Target Release:** Milestone 2 Final Evaluation

---

## 1. Executive Summary & Purpose

Nimbus implements a hierarchical cloud-edge authority model for multi-robot hospital fleets. Clinical staff submit natural language dispatch requests. The cloud orchestrator synthesizes a temporally and spatially bounded **Lease**. The edge supervisor executes subtasks autonomously within the leased envelope, surviving network partitions without stalling or safety violations.

---

## 2. Updated Table 4: Complete Functional & Non-Functional Traceability

| ID | Category | Requirement Description | Verified By | Owner | Evidence Artifact |
| :--- | :--- | :--- | :--- | :---: | :--- |
| **FR-1** | Safety | Immediate stopping on network loss ($\le 0.5$ s, safe distance $\ge 0.20$ m). | Test 1 | P1 | `results/fr1_safety_stop.csv` |
| **FR-2** | Navigation | Collision-free obstacle avoidance ($\ge 90\%$ collision-free runs). | Test 1 | P1 | `results/fr2_nav.csv` |
| **FR-3** | Edge Autonomy | Local tactical decision-making using quantized SLM + reflex fallback. | E1 | P2 | `results/e1_models.csv` |
| **FR-4** | Dynamic Events | Autonomous handling of transient obstacles, detours, and faults ($\ge 80\%$). | E2 | P2 | `data/e2_situations.json`, `e2_edge.csv` |
| **FR-5** | Geofencing | Strict spatial boundary confinement to `allowed_zones` (0 boundary breaches). | E6 | P2 | `edge/tests/test_boundary_and_parking.py` |
| **FR-6** | Watchdog & Park | Safe parking outside corridors/lifts upon lease expiry or link cut. | E4, E5 | P2 | `results/e5_edge.csv` |
| **FR-7** | Failover | Edge node crash failover to standby in $\le 30$ s with 0 duplicate actions. | E5 | P2 | `edge/tests/test_failover_takeover.py` |
| **FR-8** | Idempotence | Duplicate, re-ordered, and stale command immunity (exactly-once execution). | E6 | P3/P4 | `results/e6_injection.txt` |
| **FR-9** | Escalation | Standardized schema escalation for unexpected barriers and photo delivery proof. | E3 | P2/P3 | `results/e3/log_excerpts.md` |
| **FR-10** | Store & Forward | Local SQLite disk queue retains 100% of telemetry during WAN partition. | E5 | P2 | `results/e5_edge.csv` |
| **FR-11** | Multitenancy | Cluster isolation and selective telemetry sharing. | E6 | P4 | `results/e6_sharing.csv` |
| **FR-12** | Reconciliation | Post-outage fleet reconciliation within $\le 1$ min (0 multi-leased robots). | E5 | P2 | `results/e5_edge.csv` |
| **FR-13** | Ambiguity | Request parsing $\ge 85\%$ accuracy with clarification recall. | E2 | P3 | `results/e2_cloud.csv` |
| **FR-14** | Priority Queue | Urgent clinical work preempts routine orders in scheduling. | E3 | P3 | `results/e3/log_excerpts.md` |
| **FR-15** | Rule Checker | Deterministic invariant validation; fallback after 2 failed proposals. | E3 | P3 | `results/e3/log_excerpts.md` |
| **FR-16** | Leased Bounds | Hard-coded cryptographic temporal/spatial envelopes. | E3 | P3 | `results/e3/results.csv` |
| **FR-17** | Policy Restrict | Natural language restriction handling (isolation wards & equipment extraction).| E2 | P3 | `results/e2_cloud.csv` |
| **FR-18** | Circuit Breaker | Cloud LLM outage detection and seamless fallback to deterministic rules. | E5 | P3 | `results/e5_bedrock_down.csv` |
| **FR-19** | Habit Learning | Context-dependent resolution without redundant clarification. | E2 | P3 | `evaluation/e2/context.json` |
| **FR-20** | Audit Replay | Complete task timeline reconstruction from decision log. | E6 | P4 | `results/e6_replay.json` |
| **FR-21** | UI Dispatch | End-to-end task initiation and tracking from web interface. | E3 | P4 | `demo/nimbus_demo.mp4` |
| **FR-22** | Preemption | Fast operator cancellation and urgent override reaching robots $\le 5$ s. | E3 | P4 | `results/e3/results.csv` |
| **NFR-1** | Latency | Edge decision p95 $\le 10$ s, output token cap $\le 150$. | E1 | P2 | `results/e1_models.csv` |
| **NFR-2** | Recovery | Normal operation resumes within 1–2 min following WAN restoration. | E4, E5 | P2 | `results/e4.csv`, `e5_edge.csv` |
| **NFR-3** | Scalability | Fleet horizontal scalability to 10+ robots (Scheduled for Nov 7–15). | E7 | — | Stretch milestone |
| **NFR-4** | Cost Efficiency | C1 LLM call count $\le 50\%$ of C2 baseline (Achieved: 28.8% ratio). | E3 | All | `results/e3/cost.svg` |
| **NFR-5** | Privacy | 100% synthetic clinical data; zero real patient PII committed. | E6 | P4 | `evaluation/manifest.json` |
| **NFR-6** | Determinism | Seeded re-runs produce byte-identical decision logs (diff = empty). | E6 | P4 | `results/e6_seed_diff.txt` |

---

## 3. Formal Scope Resolutions in PRD v6.0

1. **Settlement of Experiments E4 & E5 (MVP Status Confirmed)**:
   * **Decision:** Experiments **E4** (Network Trace Replay) and **E5** (Edge Failures & Outbox Resilience) are **confirmed core MVP deliverables** for Milestone 2. 
   * **Justification:** Hospital deployments operate in environments with unavoidable radio shadows (elevator shafts, radiology shielding, basement tunnels). The demonstration of store-and-forward persistence (`results/e5_edge.csv`) and empirical campus trace playback (`results/e4.csv`) validates the core thesis of leased edge autonomy.
   * **Stretch Postponement:** Experiments E7 (Scale-10 pack), E8, and E9 remain scheduled as post-evaluation stretch milestones (7–15 November).

2. **Alignment of FR-22 (Preemption & Emergency Stop) with E3**:
   * FR-22 specifies that operator cancellations and urgent priority overrides propagate to in-flight robots within 5 seconds. In Experiment E3 Task T8, urgent preemption occurred in $1.12$ seconds, fully satisfying this requirement.

3. **Cross-Referencing of Duplicate FR/NFR Pairs**:
   * **FR-3 / FR-4 $\leftrightarrow$ NFR-1**: Local tactical autonomy is bounded by NFR-1's $\le 10$s p95 SLA (achieved: $0.495$s on pinned model `qwen2.5:1.5b`).
   * **FR-6 / FR-10 $\leftrightarrow$ NFR-2**: Edge outbox store-and-forward guarantees 0 lost messages and recovery within $68.0$s (meeting the 1–2 min NFR-2 target).
   * **FR-5 / FR-8 $\leftrightarrow$ NFR-6**: Invariant enforcement and message deduplication ensure that seeded executions produce deterministic, byte-identical decision timelines.
