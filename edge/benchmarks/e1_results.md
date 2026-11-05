# Nimbus Experiment E1 Benchmark Results

- **Model:** `qwen2.5:1.5b` (4-bit quant, 150-token cap)
- **Host:** Linux x86_64 (x86_64)
- **Engine Mode:** `ollama`
- **Date:** 2026-11-05 16:17:06
- **Workload:** 15 decisions over 5 hospital tactical scenarios

## Latency Percentiles (NFR-1 Audit)

| Metric | Measured Value | NFR-1 Target | Compliance |
|:---|:---|:---|:---|
| **Min Latency** | `0.000 s` | N/A | OK |
| **Median (p50)** | `3.028 s` | N/A | OK |
| **Mean** | `3.776 s` | N/A | OK |
| **p95 Latency** | **`22.022 s`** | **$\le 10.0$ s** | **FAIL** |
| **Max Latency** | `22.022 s` | N/A | OK |
| **JSON Syntax Adherence** | **`100.0%`** | **100%** | **OK** |

## Trial Breakdown

| Trial | Scenario ID | Description | Decided By | Action | Latency (s) |
|:---:|:---:|:---|:---:|:---:|:---:|
| 1 | SC-01 | Routine waypoint progression in clear corridor | `SLM` | `LOCAL_WAIT` | 1.65 s |
| 1 | SC-02 | Dynamic obstacle: maintenance cart in corridor | `SLM` | `LOCAL_WAIT` | 3.03 s |
| 1 | SC-03 | Instruction ambiguity: multiple equipment racks in approach | `SLM` | `ESCALATE_UNEXPECTED` | 3.90 s |
| 1 | SC-04 | Corridor blocked, no alternate route inside allowed zones | `SLM` | `LOCAL_WAIT` | 3.61 s |
| 1 | SC-05 | Lease expiry imminent (<30s remaining) | `RULE` | `SAFE_PARK` | 0.00 s |
| 2 | SC-01 | Routine waypoint progression in clear corridor | `SLM` | `LOCAL_WAIT` | 3.46 s |
| 2 | SC-02 | Dynamic obstacle: maintenance cart in corridor | `SLM` | `LOCAL_WAIT` | 4.93 s |
| 2 | SC-03 | Instruction ambiguity: multiple equipment racks in approach | `SLM` | `ESCALATE_UNEXPECTED` | 2.57 s |
| 2 | SC-04 | Corridor blocked, no alternate route inside allowed zones | `SLM` | `LOCAL_WAIT` | 2.26 s |
| 2 | SC-05 | Lease expiry imminent (<30s remaining) | `RULE` | `SAFE_PARK` | 0.00 s |
| 3 | SC-01 | Routine waypoint progression in clear corridor | `SLM` | `LOCAL_WAIT` | 3.39 s |
| 3 | SC-02 | Dynamic obstacle: maintenance cart in corridor | `SLM` | `LOCAL_WAIT` | 4.10 s |
| 3 | SC-03 | Instruction ambiguity: multiple equipment racks in approach | `SLM` | `ESCALATE_UNEXPECTED` | 1.73 s |
| 3 | SC-04 | Corridor blocked, no alternate route inside allowed zones | `SLM` | `ESCALATE_UNEXPECTED` | 22.02 s |
| 3 | SC-05 | Lease expiry imminent (<30s remaining) | `RULE` | `SAFE_PARK` | 0.00 s |
