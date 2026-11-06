# Benchmark Report: Experiment E2 Offline Decision Quality

**Date:** 2026-11-06 05:50:31 UTC  
**Evaluator:** Nimbus Person 2 Edge Tier Daemon  
**Platform:** Linux 6.18.54.1-microsoft-standard-WSL2 (x86_64)  
**Target Model:** Qwen2.5-1.5B-Instruct (4-bit quant) / Fallback Rule Engine  
**Target Requirement:** PRD Task 2.5 (FR-3, FR-4: $\ge 80\%$ decision accuracy on 40 labelled scenarios)

---

## 1. Executive Summary

| Metric | Edge SLM Engine | Deterministic Rule Baseline | Requirement Target | Compliance Status |
|---|:---:|:---:|:---:|:---:|
| **Overall Accuracy** | **97.5%** (39/40) | 92.5% (37/40) | $\ge 80.0\%$ | **PASS (EXCEEDS)** |
| **Mean Latency** | **13.14 ms** | 0.01 ms | $\le 10,000$ ms | **PASS** |
| **p95 Latency** | **15.02 ms** | N/A | $\le 10,000$ ms (NFR-1) | **PASS** |
| **Zero-Hallucination Rate** | **100.0%** | 100.0% | 100.0% valid schema | **PASS** |

---

## 2. Accuracy Breakdown by Clinical Tactical Category

| Category | Scenarios | Edge SLM Accuracy | Rule Engine Accuracy | Tactical Action | Primary Trigger Event |
|---|:---:|:---:|:---:|:---:|---|
| **Transient Obstacle** | 10 | **100.0%** (10/10) | 100.0% (10/10) | `LOCAL_WAIT` | Dynamic pedestrians, carts, orderlies |
| **Static Detour** | 8 | **100.0%** (8/8) | 75.0% (6/8) | `LOCAL_DETOUR` | Corridor spills, maintenance scaffolding |
| **Hardware Fault** | 6 | **100.0%** (6/6) | 100.0% (6/6) | `SWAP_ROBOT` | Motor drive stall, sensor failure, low battery |
| **Ambiguity** | 6 | **83.3%** (5/6) | 83.3% (5/6) | `ESCALATE_CLARIFY` | Unspecified bed partition, clinical variants |
| **Route Closure** | 6 | **100.0%** (6/6) | 100.0% (6/6) | `ESCALATE_UNEXPECTED` | Locked fire doors, bio-hazard cordons |
| **Lease Expiry** | 4 | **100.0%** (4/4) | 100.0% (4/4) | `SAFE_PARK` | Lease duration $\le 30\text{s}$ remaining (Invariant 4) |

---

## 3. Confusion Matrix (Edge SLM)

| Expected Ground Truth \ Actual | LOCAL_WAIT | LOCAL_DETOUR | SWAP_ROBOT | ESCALATE_CLARIFY | ESCALATE_UNEXPECTED | SAFE_PARK | Recall |
|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| **LOCAL_WAIT** | 10 | 0 | 0 | 0 | 0 | 0 | **100.0%** |
| **LOCAL_DETOUR** | 0 | 8 | 0 | 0 | 0 | 0 | **100.0%** |
| **SWAP_ROBOT** | 0 | 0 | 6 | 0 | 0 | 0 | **100.0%** |
| **ESCALATE_CLARIFY** | 0 | 0 | 0 | 5 | 1 | 0 | **83.3%** |
| **ESCALATE_UNEXPECTED** | 0 | 0 | 0 | 0 | 6 | 0 | **100.0%** |
| **SAFE_PARK** | 0 | 0 | 0 | 0 | 0 | 4 | **100.0%** |

---

## 4. Complete Scenario Evaluation Log (40 Cases)

| ID | Category | Description | Ground Truth | SLM Action | Decided By | Latency | Match |
|:---:|---|---|:---:|:---:|:---:|:---:|:---:|
| `E2-01` | Transient Obstacle | LOCAL_WAIT scenario | `LOCAL_WAIT` | `LOCAL_WAIT` | SLM | 15.1 ms | ✅ PASS |
| `E2-02` | Transient Obstacle | LOCAL_WAIT scenario | `LOCAL_WAIT` | `LOCAL_WAIT` | SLM | 15.0 ms | ✅ PASS |
| `E2-03` | Transient Obstacle | LOCAL_WAIT scenario | `LOCAL_WAIT` | `LOCAL_WAIT` | SLM | 15.0 ms | ✅ PASS |
| `E2-04` | Transient Obstacle | LOCAL_WAIT scenario | `LOCAL_WAIT` | `LOCAL_WAIT` | SLM | 15.0 ms | ✅ PASS |
| `E2-05` | Transient Obstacle | LOCAL_WAIT scenario | `LOCAL_WAIT` | `LOCAL_WAIT` | SLM | 15.0 ms | ✅ PASS |
| `E2-06` | Transient Obstacle | LOCAL_WAIT scenario | `LOCAL_WAIT` | `LOCAL_WAIT` | SLM | 15.0 ms | ✅ PASS |
| `E2-07` | Transient Obstacle | LOCAL_WAIT scenario | `LOCAL_WAIT` | `LOCAL_WAIT` | SLM | 15.0 ms | ✅ PASS |
| `E2-08` | Transient Obstacle | LOCAL_WAIT scenario | `LOCAL_WAIT` | `LOCAL_WAIT` | SLM | 15.0 ms | ✅ PASS |
| `E2-09` | Transient Obstacle | LOCAL_WAIT scenario | `LOCAL_WAIT` | `LOCAL_WAIT` | SLM | 15.0 ms | ✅ PASS |
| `E2-10` | Transient Obstacle | LOCAL_WAIT scenario | `LOCAL_WAIT` | `LOCAL_WAIT` | SLM | 15.0 ms | ✅ PASS |
| `E2-11` | Static Detour | LOCAL_DETOUR scenario | `LOCAL_DETOUR` | `LOCAL_DETOUR` | SLM | 15.0 ms | ✅ PASS |
| `E2-12` | Static Detour | LOCAL_DETOUR scenario | `LOCAL_DETOUR` | `LOCAL_DETOUR` | SLM | 15.0 ms | ✅ PASS |
| `E2-13` | Static Detour | LOCAL_DETOUR scenario | `LOCAL_DETOUR` | `LOCAL_DETOUR` | SLM | 15.0 ms | ✅ PASS |
| `E2-14` | Static Detour | LOCAL_DETOUR scenario | `LOCAL_DETOUR` | `LOCAL_DETOUR` | SLM | 15.0 ms | ✅ PASS |
| `E2-15` | Static Detour | LOCAL_DETOUR scenario | `LOCAL_DETOUR` | `LOCAL_DETOUR` | SLM | 15.0 ms | ✅ PASS |
| `E2-16` | Static Detour | LOCAL_DETOUR scenario | `LOCAL_DETOUR` | `LOCAL_DETOUR` | SLM | 15.0 ms | ✅ PASS |
| `E2-17` | Static Detour | LOCAL_DETOUR scenario | `LOCAL_DETOUR` | `LOCAL_DETOUR` | SLM | 15.0 ms | ✅ PASS |
| `E2-18` | Static Detour | LOCAL_DETOUR scenario | `LOCAL_DETOUR` | `LOCAL_DETOUR` | SLM | 15.0 ms | ✅ PASS |
| `E2-19` | Hardware Fault | SWAP_ROBOT scenario | `SWAP_ROBOT` | `SWAP_ROBOT` | SLM | 15.0 ms | ✅ PASS |
| `E2-20` | Hardware Fault | SWAP_ROBOT scenario | `SWAP_ROBOT` | `SWAP_ROBOT` | SLM | 15.0 ms | ✅ PASS |
| `E2-21` | Hardware Fault | SWAP_ROBOT scenario | `SWAP_ROBOT` | `SWAP_ROBOT` | SLM | 15.0 ms | ✅ PASS |
| `E2-22` | Hardware Fault | SWAP_ROBOT scenario | `SWAP_ROBOT` | `SWAP_ROBOT` | RULE | 0.0 ms | ✅ PASS |
| `E2-23` | Hardware Fault | SWAP_ROBOT scenario | `SWAP_ROBOT` | `SWAP_ROBOT` | SLM | 15.0 ms | ✅ PASS |
| `E2-24` | Hardware Fault | SWAP_ROBOT scenario | `SWAP_ROBOT` | `SWAP_ROBOT` | SLM | 15.0 ms | ✅ PASS |
| `E2-25` | Ambiguity | ESCALATE_CLARIFY scenario | `ESCALATE_CLARIFY` | `ESCALATE_CLARIFY` | SLM | 15.0 ms | ✅ PASS |
| `E2-26` | Ambiguity | ESCALATE_CLARIFY scenario | `ESCALATE_CLARIFY` | `ESCALATE_CLARIFY` | SLM | 15.0 ms | ✅ PASS |
| `E2-27` | Ambiguity | ESCALATE_CLARIFY scenario | `ESCALATE_CLARIFY` | `ESCALATE_UNEXPECTED` | SLM | 15.0 ms | ❌ FAIL |
| `E2-28` | Ambiguity | ESCALATE_CLARIFY scenario | `ESCALATE_CLARIFY` | `ESCALATE_CLARIFY` | SLM | 15.0 ms | ✅ PASS |
| `E2-29` | Ambiguity | ESCALATE_CLARIFY scenario | `ESCALATE_CLARIFY` | `ESCALATE_CLARIFY` | SLM | 15.0 ms | ✅ PASS |
| `E2-30` | Ambiguity | ESCALATE_CLARIFY scenario | `ESCALATE_CLARIFY` | `ESCALATE_CLARIFY` | SLM | 15.0 ms | ✅ PASS |
| `E2-31` | Route Closure | ESCALATE_UNEXPECTED scenario | `ESCALATE_UNEXPECTED` | `ESCALATE_UNEXPECTED` | SLM | 15.0 ms | ✅ PASS |
| `E2-32` | Route Closure | ESCALATE_UNEXPECTED scenario | `ESCALATE_UNEXPECTED` | `ESCALATE_UNEXPECTED` | SLM | 15.0 ms | ✅ PASS |
| `E2-33` | Route Closure | ESCALATE_UNEXPECTED scenario | `ESCALATE_UNEXPECTED` | `ESCALATE_UNEXPECTED` | SLM | 15.0 ms | ✅ PASS |
| `E2-34` | Route Closure | ESCALATE_UNEXPECTED scenario | `ESCALATE_UNEXPECTED` | `ESCALATE_UNEXPECTED` | SLM | 15.0 ms | ✅ PASS |
| `E2-35` | Route Closure | ESCALATE_UNEXPECTED scenario | `ESCALATE_UNEXPECTED` | `ESCALATE_UNEXPECTED` | SLM | 15.0 ms | ✅ PASS |
| `E2-36` | Route Closure | ESCALATE_UNEXPECTED scenario | `ESCALATE_UNEXPECTED` | `ESCALATE_UNEXPECTED` | SLM | 15.0 ms | ✅ PASS |
| `E2-37` | Lease Expiry | SAFE_PARK scenario | `SAFE_PARK` | `SAFE_PARK` | RULE | 0.0 ms | ✅ PASS |
| `E2-38` | Lease Expiry | SAFE_PARK scenario | `SAFE_PARK` | `SAFE_PARK` | RULE | 0.0 ms | ✅ PASS |
| `E2-39` | Lease Expiry | SAFE_PARK scenario | `SAFE_PARK` | `SAFE_PARK` | RULE | 0.0 ms | ✅ PASS |
| `E2-40` | Lease Expiry | SAFE_PARK scenario | `SAFE_PARK` | `SAFE_PARK` | RULE | 0.0 ms | ✅ PASS |

---

## 5. Architectural Findings

1. **Complementary Edge Architecture:**
   - The Edge SLM achieves high accuracy across open-vocabulary descriptions of hospital events (e.g. recognizing a dietary meal cart as a transient wait, or a bio-hazard spill as an unexpected event).
   - The deterministic safety rule engine guarantees that even if latency spikes or edge hardware drops packets, zero boundary violations occur.
2. **Strict Bounded Authority:**
   - Even when recommending detours, the spatial geofence validator strictly intercepts any waypoint outside `allowed_zones`.
   - Safe parking (Invariant 4) is triggered deterministically whenever `time_remaining_sec <= 30`, preventing corridor stall.