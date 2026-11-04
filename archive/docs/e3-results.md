# E3 results: C1 vs C2 vs C3 on T1-T8

Map `hospital_sim`, model **stand-in**, 10 seeded runs per cell (medians). Success, model calls, tokens and WAN bytes are measured from the real code; completion time and latency are MODELLED (constants in `scripts/run_e3.py`: model call 2.5 s, round trip 80 ms, edge decision 0.5 s). **The model here is the offline stand-in, so token counts are estimates and no model-quality conclusion should be drawn.**

| Task | Config | Success | Deadline met | Model calls | WAN bytes | Cost / task (USD) | Modelled completion (s) |
|---|---|---|---|---|---|---|---|
| T1 Routine delivery | C1 | 100% | 100% | 2 | 1534 | 9.67e-05 | 93 |
| T1  | C2 | 100% | 100% | 8 | 5134 | 3.00e-04 | 122 |
| T1  | C3 | 100% | 100% | 0 | 1534 | 2.00e-07 | 85 |
| T2 Urgent under background traffic | C1 | 100% | 100% | 1 | 1532 | 5.28e-05 | 91 |
| T2  | C2 | 100% | 100% | 7 | 5132 | 2.56e-04 | 119 |
| T2  | C3 | 100% | 100% | 0 | 1532 | 2.00e-07 | 85 |
| T3 Vague request | C1 | 100% | 100% | 2 | 2037 | 1.06e-04 | 138 |
| T3  | C2 | 100% | 100% | 8 | 5637 | 3.13e-04 | 167 |
| T3  | C3 | 100% | 100% | 0 | 2037 | 4.00e-07 | 130 |
| T4 Blocked corridor, detour inside the lease | C1 | 100% | 100% | 1 | 2499 | 5.70e-05 | 175 |
| T4  | C2 | 100% | 100% | 7 | 6099 | 2.78e-04 | 204 |
| T4  | C3 | 100% | 100% | 0 | 2499 | 2.00e-07 | 170 |
| T5 Gate closed: cross-wing hand-over | C1 | 100% | 100% | 2 | 6643 | 9.87e-05 | 109 |
| T5  | C2 | 100% | 100% | 8 | 10243 | 3.06e-04 | 137 |
| T5  | C3 | 100% | 100% | 0 | 6643 | 6.00e-07 | 101 |
| T6 Ward declared infectious mid-mission | C1 | 100% | 100% | 2 | 2787 | 1.18e-04 | 135 |
| T6  | C2 | 100% | 100% | 2 | 2787 | 1.18e-04 | 135 |
| T6  | C3 | 100% | 100% | 0 | 2787 | 6.00e-07 | 130 |
| T7 Inventory sweep (survey) | C1 | 100% | 100% | 1 | 2342 | 4.82e-05 | 211 |
| T7  | C2 | 100% | 100% | 8 | 606540 | 2.92e-04 | 263 |
| T7  | C3 | 100% | 100% | 0 | 602340 | 2.00e-07 | 205 |
| T8 Three requests, two robots | C1 | 100% | 100% | 4 | 4602 | 2.02e-04 | 10 |
| T8  | C2 | 100% | 100% | 4 | 4602 | 2.02e-04 | 10 |
| T8  | C3 | 100% | 100% | 0 | 4602 | 8.00e-07 | 0 |

**NFR-4** (C1's model calls per task at most 50% of C2's): C1 150 vs C2 520, ratio **0.288** (met).

Notes: T7's WAN bytes differ because only C1 has an edge model to filter what the robots saw (the PRD expects C1 to use the least bandwidth); C2 and C3 ship every photo's result. T6's cloud decisions are the same in C1 and C2 because the edge is not involved until the recall reaches it.

## Plots

![success](../results/e3/success.svg)
![llm_calls](../results/e3/llm_calls.svg)
![cost](../results/e3/cost.svg)
![wan_bytes](../results/e3/wan_bytes.svg)
![completion](../results/e3/completion.svg)
![tradeoff](../results/e3/tradeoff.svg)
