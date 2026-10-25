# Nimbus Experiment E1 Benchmark Results

- **Model:** `qwen2.5:1.5b` (4-bit quant, 150-token cap)
- **Host:** Windows AMD64 (Intel64 Family 6 Model 170 Stepping 4, GenuineIntel)
- **Engine Mode:** `ollama`
- **Date:** 2026-10-25 14:17:52
- **Workload:** 15 decisions over 5 hospital tactical scenarios

## Latency Percentiles (NFR-1 Audit)

| Metric | Measured Value | NFR-1 Target | Compliance |
|:---|:---|:---|:---|
| **Min Latency** | `0.000 s` | N/A | OK |
| **Median (p50)** | `1.488 s` | N/A | OK |
| **Mean** | `1.483 s` | N/A | OK |
| **p95 Latency** | **`3.157 s`** | **$\le 10.0$ s** | **PASS** |
| **Max Latency** | `3.157 s` | N/A | OK |
| **JSON Syntax Adherence** | **`100.0%`** | **100%** | **OK** |

## Trial Breakdown

| Trial | Scenario ID | Description | Decided By | Action | Latency (s) |
|:---:|:---:|:---|:---:|:---:|:---:|
| 1 | SC-01 | Routine waypoint progression in clear corridor | `SLM` | `DISPATCH_STEP` | 1.27 s |
| 1 | SC-02 | Dynamic obstacle: maintenance cart in corridor | `SLM` | `LOCAL_DETOUR` | 2.17 s |
| 1 | SC-03 | Instruction ambiguity: multiple equipment racks in approach | `SLM` | `LOCAL_WAIT` | 3.16 s |
| 1 | SC-04 | Corridor blocked, no alternate route inside allowed zones | `SLM` | `LOCAL_DETOUR` | 2.12 s |
| 1 | SC-05 | Lease expiry imminent (<30s remaining) | `RULE` | `SAFE_PARK` | 0.00 s |
| 2 | SC-01 | Routine waypoint progression in clear corridor | `SLM` | `DISPATCH_STEP` | 1.24 s |
| 2 | SC-02 | Dynamic obstacle: maintenance cart in corridor | `SLM` | `LOCAL_DETOUR` | 1.35 s |
| 2 | SC-03 | Instruction ambiguity: multiple equipment racks in approach | `SLM` | `LOCAL_WAIT` | 2.22 s |
| 2 | SC-04 | Corridor blocked, no alternate route inside allowed zones | `SLM` | `LOCAL_DETOUR` | 1.49 s |
| 2 | SC-05 | Lease expiry imminent (<30s remaining) | `RULE` | `SAFE_PARK` | 0.00 s |
| 3 | SC-01 | Routine waypoint progression in clear corridor | `SLM` | `DISPATCH_STEP` | 1.51 s |
| 3 | SC-02 | Dynamic obstacle: maintenance cart in corridor | `SLM` | `LOCAL_DETOUR` | 1.37 s |
| 3 | SC-03 | Instruction ambiguity: multiple equipment racks in approach | `SLM` | `LOCAL_WAIT` | 2.55 s |
| 3 | SC-04 | Corridor blocked, no alternate route inside allowed zones | `SLM` | `LOCAL_DETOUR` | 1.79 s |
| 3 | SC-05 | Lease expiry imminent (<30s remaining) | `RULE` | `SAFE_PARK` | 0.00 s |
