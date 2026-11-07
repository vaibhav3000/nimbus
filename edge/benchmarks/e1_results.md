# Nimbus Experiment E1 Benchmark Results

- **Model:** `qwen2.5:1.5b` (4-bit quant, 150-token cap)
- **Host:** Windows AMD64 (Intel64 Family 6 Model 170 Stepping 4, GenuineIntel)
- **Engine Mode:** `ollama`
- **Date:** 2026-11-07 20:51:15
- **Workload:** 20 decisions over 5 hospital tactical scenarios

## Latency & Constraint Audit (NFR-1 & FR-3)

| Metric | Measured Value | Target | Compliance |
|:---|:---|:---|:---|
| **Min Latency** | `0.000 s` | N/A | OK |
| **Median (p50)** | `2.036 s` | N/A | OK |
| **Mean** | `1.581 s` | N/A | OK |
| **p95 Latency** | **`2.601 s`** | **<= 10.0 s** | **PASS** |
| **Max Latency** | `2.605 s` | N/A | OK |
| **Max Output Tokens** | **`52 tok`** | **<= 150 tok** | **PASS** |
| **JSON Syntax Adherence** | **`100.0%`** | **100%** | **PASS** |
| **FR-3 Lease Boundary Safety** | **`100.0%`** | **100%** | **PASS** |

## Trial Breakdown

| Trial | Scenario ID | Description | Decided By | Action | Est. Tokens | Latency (s) |
|:---:|:---:|:---|:---:|:---:|:---:|:---:|
| 1 | SC-01 | Routine waypoint progression in clear corridor | `SLM` | `LOCAL_WAIT` | 30 | 1.15 s |
| 1 | SC-02 | Dynamic obstacle: maintenance cart in corridor | `SLM` | `LOCAL_WAIT` | 52 | 2.60 s |
| 1 | SC-03 | Instruction ambiguity: multiple equipment racks in approach | `SLM` | `ESCALATE_UNEXPECTED` | 38 | 2.28 s |
| 1 | SC-04 | Corridor blocked, no alternate route inside allowed zones | `SLM` | `LOCAL_WAIT` | 34 | 1.98 s |
| 1 | SC-05 | Lease expiry imminent (<30s remaining) | `RULE` | `SAFE_PARK` | 30 | 0.00 s |
| 2 | SC-01 | Routine waypoint progression in clear corridor | `SLM` | `ESCALATE_UNEXPECTED` | 33 | 2.15 s |
| 2 | SC-02 | Dynamic obstacle: maintenance cart in corridor | `SLM` | `LOCAL_WAIT` | 31 | 2.16 s |
| 2 | SC-03 | Instruction ambiguity: multiple equipment racks in approach | `SLM` | `ESCALATE_UNEXPECTED` | 38 | 1.47 s |
| 2 | SC-04 | Corridor blocked, no alternate route inside allowed zones | `SLM` | `LOCAL_WAIT` | 39 | 2.26 s |
| 2 | SC-05 | Lease expiry imminent (<30s remaining) | `RULE` | `SAFE_PARK` | 30 | 0.00 s |
| 3 | SC-01 | Routine waypoint progression in clear corridor | `SLM` | `ESCALATE_UNEXPECTED` | 35 | 2.34 s |
| 3 | SC-02 | Dynamic obstacle: maintenance cart in corridor | `SLM` | `LOCAL_WAIT` | 32 | 2.09 s |
| 3 | SC-03 | Instruction ambiguity: multiple equipment racks in approach | `SLM` | `ESCALATE_UNEXPECTED` | 40 | 1.48 s |
| 3 | SC-04 | Corridor blocked, no alternate route inside allowed zones | `SLM` | `LOCAL_WAIT` | 34 | 1.24 s |
| 3 | SC-05 | Lease expiry imminent (<30s remaining) | `RULE` | `SAFE_PARK` | 30 | 0.00 s |
| 4 | SC-01 | Routine waypoint progression in clear corridor | `SLM` | `LOCAL_WAIT` | 30 | 2.32 s |
| 4 | SC-02 | Dynamic obstacle: maintenance cart in corridor | `SLM` | `LOCAL_WAIT` | 46 | 2.52 s |
| 4 | SC-03 | Instruction ambiguity: multiple equipment racks in approach | `SLM` | `ESCALATE_UNEXPECTED` | 40 | 1.48 s |
| 4 | SC-04 | Corridor blocked, no alternate route inside allowed zones | `SLM` | `LOCAL_WAIT` | 34 | 2.12 s |
| 4 | SC-05 | Lease expiry imminent (<30s remaining) | `RULE` | `SAFE_PARK` | 30 | 0.00 s |
