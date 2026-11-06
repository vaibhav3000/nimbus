# E3 results: C1 vs C2 vs C3 on T1-T8

Map `hospital_sim6`, model **stand-in**, 10 seeded runs per cell (medians). Success, model calls, tokens and WAN bytes are measured from the real code; completion time and latency are MODELLED (constants in `scripts/run_e3.py`: model call 2.5 s, round trip 80 ms, edge decision 0.5 s). **The model here is the offline stand-in, so token counts are estimates and no model-quality conclusion should be drawn.**

| Task | Config | Success | Deadline met | Model calls | WAN bytes | Cost / task (USD) | Modelled completion (s) |
|---|---|---|---|---|---|---|---|
| T1 Routine delivery | C1 | 100% | 100% | 2 | 1830 | 1.00e-04 | 66 |
| T1  | C2 | 100% | 100% | 8 | 5430 | 3.10e-04 | 94 |
| T1  | C3 | 100% | 100% | 0 | 1830 | 2.00e-07 | 58 |
| T2 Urgent under background traffic | C1 | 100% | 100% | 2 | 2130 | 9.76e-05 | 82 |
| T2  | C2 | 100% | 100% | 8 | 5730 | 3.11e-04 | 110 |
| T2  | C3 | 100% | 100% | 0 | 2130 | 2.00e-07 | 74 |
| T3 Vague request | C1 | 100% | 100% | 2 | 2372 | 1.06e-04 | 96 |
| T3  | C2 | 100% | 100% | 8 | 5972 | 3.20e-04 | 124 |
| T3  | C3 | 100% | 100% | 0 | 2372 | 4.00e-07 | 88 |
| T4 Blocked corridor, detour inside the lease | C1 | 100% | 100% | 1 | 2140 | 5.74e-05 | 48 |
| T4  | C2 | 100% | 100% | 7 | 5740 | 2.76e-04 | 77 |
| T4  | C3 | 100% | 100% | 0 | 2140 | 2.00e-07 | 43 |
| T5 Gate closed: cross-wing hand-over | C1 | 100% | 100% | 2 | 5886 | 9.88e-05 | 58 |
| T5  | C2 | 100% | 100% | 8 | 9486 | 3.08e-04 | 87 |
| T5  | C3 | 100% | 100% | 0 | 5886 | 6.00e-07 | 50 |
| T6 Ward declared infectious mid-mission | C1 | 100% | 100% | 2 | 2939 | 1.19e-04 | 77 |
| T6  | C2 | 100% | 100% | 2 | 2939 | 1.19e-04 | 77 |
| T6  | C3 | 100% | 100% | 0 | 2939 | 6.00e-07 | 72 |
| T7 Inventory sweep (survey) | C1 | 100% | 100% | 1 | 2777 | 4.86e-05 | 98 |
| T7  | C2 | 100% | 100% | 8 | 606980 | 3.02e-04 | 150 |
| T7  | C3 | 100% | 100% | 0 | 602780 | 2.00e-07 | 92 |
| T8 Three requests, two robots | C1 | 0% | 0% | 6 | 5928 | 2.96e-04 | 15 |
| T8  | C2 | 0% | 0% | 6 | 5928 | 2.96e-04 | 15 |
| T8  | C3 | 0% | 0% | 0 | 5928 | 6.00e-07 | 0 |

**NFR-4** (C1's model calls per task at most 50% of C2's): C1 180 vs C2 550, ratio **0.327** (met).

Notes: T7's WAN bytes differ because only C1 has an edge model to filter what the robots saw (the PRD expects C1 to use the least bandwidth); C2 and C3 ship every photo's result. T6's cloud decisions are the same in C1 and C2 because the edge is not involved until the recall reaches it.

## Plots

![success](../results/e3/success.svg)
![llm_calls](../results/e3/llm_calls.svg)
![cost](../results/e3/cost.svg)
![wan_bytes](../results/e3/wan_bytes.svg)
![completion](../results/e3/completion.svg)
![tradeoff](../results/e3/tradeoff.svg)
