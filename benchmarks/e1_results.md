# Nimbus Experiment E1 Benchmark Results

- **Model:** `qwen2.5:1.5b` (4-bit quant, 150-token cap)
- **Host:** Linux x86_64 (x86_64)
- **Engine Mode:** `mock`
- **Date:** 2026-11-06 05:54:11
- **Workload:** 20 decisions over 5 hospital tactical scenarios

## Latency & Constraint Audit (NFR-1 & FR-3)

| Metric | Measured Value | Target | Compliance |
|:---|:---|:---|:---|
| **Min Latency** | `0.000 s` | N/A | OK |
| **Median (p50)** | `0.015 s` | N/A | OK |
| **Mean** | `0.012 s` | N/A | OK |
| **p95 Latency** | **`0.015 s`** | **<= 10.0 s** | **PASS** |
| **Max Latency** | `0.015 s` | N/A | OK |
| **Max Output Tokens** | **`40 tok`** | **<= 150 tok** | **PASS** |
| **JSON Syntax Adherence** | **`100.0%`** | **100%** | **PASS** |
| **FR-3 Lease Boundary Safety** | **`100.0%`** | **100%** | **PASS** |

## Trial Breakdown

| Trial | Scenario ID | Description | Decided By | Action | Est. Tokens | Latency (s) |
|:---:|:---:|:---|:---:|:---:|:---:|:---:|
| 1 | SC-01 | Routine waypoint progression in clear corridor | `SLM` | `DISPATCH_STEP` | 30 | 0.02 s |
| 1 | SC-02 | Dynamic obstacle: maintenance cart in corridor | `SLM` | `LOCAL_WAIT` | 33 | 0.02 s |
| 1 | SC-03 | Instruction ambiguity: multiple equipment racks in approach | `SLM` | `ESCALATE_CLARIFY` | 38 | 0.02 s |
| 1 | SC-04 | Corridor blocked, no alternate route inside allowed zones | `SLM` | `ESCALATE_UNEXPECTED` | 40 | 0.02 s |
| 1 | SC-05 | Lease expiry imminent (<30s remaining) | `RULE` | `SAFE_PARK` | 26 | 0.00 s |
| 2 | SC-01 | Routine waypoint progression in clear corridor | `SLM` | `DISPATCH_STEP` | 30 | 0.02 s |
| 2 | SC-02 | Dynamic obstacle: maintenance cart in corridor | `SLM` | `LOCAL_WAIT` | 33 | 0.02 s |
| 2 | SC-03 | Instruction ambiguity: multiple equipment racks in approach | `SLM` | `ESCALATE_CLARIFY` | 38 | 0.02 s |
| 2 | SC-04 | Corridor blocked, no alternate route inside allowed zones | `SLM` | `ESCALATE_UNEXPECTED` | 40 | 0.02 s |
| 2 | SC-05 | Lease expiry imminent (<30s remaining) | `RULE` | `SAFE_PARK` | 26 | 0.00 s |
| 3 | SC-01 | Routine waypoint progression in clear corridor | `SLM` | `DISPATCH_STEP` | 30 | 0.02 s |
| 3 | SC-02 | Dynamic obstacle: maintenance cart in corridor | `SLM` | `LOCAL_WAIT` | 33 | 0.02 s |
| 3 | SC-03 | Instruction ambiguity: multiple equipment racks in approach | `SLM` | `ESCALATE_CLARIFY` | 38 | 0.02 s |
| 3 | SC-04 | Corridor blocked, no alternate route inside allowed zones | `SLM` | `ESCALATE_UNEXPECTED` | 40 | 0.02 s |
| 3 | SC-05 | Lease expiry imminent (<30s remaining) | `RULE` | `SAFE_PARK` | 26 | 0.00 s |
| 4 | SC-01 | Routine waypoint progression in clear corridor | `SLM` | `DISPATCH_STEP` | 30 | 0.02 s |
| 4 | SC-02 | Dynamic obstacle: maintenance cart in corridor | `SLM` | `LOCAL_WAIT` | 33 | 0.02 s |
| 4 | SC-03 | Instruction ambiguity: multiple equipment racks in approach | `SLM` | `ESCALATE_CLARIFY` | 38 | 0.02 s |
| 4 | SC-04 | Corridor blocked, no alternate route inside allowed zones | `SLM` | `ESCALATE_UNEXPECTED` | 40 | 0.02 s |
| 4 | SC-05 | Lease expiry imminent (<30s remaining) | `RULE` | `SAFE_PARK` | 26 | 0.00 s |
