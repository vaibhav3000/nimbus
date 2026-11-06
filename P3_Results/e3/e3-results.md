# E3 results: C1 vs C2 vs C3 on T1-T8

Map `hospital_sim`, model **stand-in**, 10 seeded runs per cell (medians). Success, model calls, tokens and WAN bytes are measured from the real code; completion time and latency are MODELLED (constants in `scripts/run_e3.py`: model call 2.5 s, round trip 80 ms, edge decision 0.5 s). **The model here is the offline stand-in, so token counts are estimates and no model-quality conclusion should be drawn.**

| Task | Config | Success | Deadline met | Model calls | WAN bytes | Cost / task (USD) | Modelled completion (s) |
|---|---|---|---|---|---|---|---|
| T1 Routine delivery | C1 | 100% | 100% | 2 | 1532 | 9.64e-05 | 54 |
| T1  | C2 | 100% | 100% | 8 | 5132 | 3.00e-04 | 82 |
| T1  | C3 | 100% | 100% | 0 | 1532 | 2.00e-07 | 46 |
| T2 Urgent under background traffic | C1 | 100% | 100% | 1 | 1530 | 5.26e-05 | 52 |
| T2  | C2 | 100% | 100% | 7 | 5130 | 2.56e-04 | 80 |
| T2  | C3 | 100% | 100% | 0 | 1530 | 2.00e-07 | 46 |
| T3 Vague request | C1 | 100% | 100% | 2 | 2238 | 1.06e-04 | 69 |
| T3  | C2 | 100% | 100% | 8 | 5838 | 3.15e-04 | 97 |
| T3  | C3 | 100% | 100% | 0 | 2238 | 4.00e-07 | 61 |
| T4 Blocked corridor, detour inside the lease | C1 | 100% | 100% | 1 | 2673 | 5.70e-05 | 76 |
| T4  | C2 | 100% | 100% | 7 | 6273 | 2.81e-04 | 104 |
| T4  | C3 | 100% | 100% | 0 | 2673 | 2.00e-07 | 70 |
| T5 Gate closed: cross-wing hand-over | C1 | 100% | 100% | 2 | 6989 | 9.91e-05 | 58 |
| T5  | C2 | 100% | 100% | 8 | 10589 | 3.06e-04 | 87 |
| T5  | C3 | 100% | 100% | 0 | 6989 | 6.00e-07 | 50 |
| T6 Ward declared infectious mid-mission | C1 | 100% | 100% | 2 | 2988 | 1.18e-04 | 66 |
| T6  | C2 | 100% | 100% | 2 | 2988 | 1.18e-04 | 66 |
| T6  | C3 | 100% | 100% | 0 | 2988 | 6.00e-07 | 60 |
| T7 Inventory sweep (survey) | C1 | 100% | 100% | 1 | 2518 | 4.82e-05 | 64 |
| T7  | C2 | 100% | 100% | 8 | 606716 | 2.95e-04 | 115 |
| T7  | C3 | 100% | 100% | 0 | 602516 | 2.00e-07 | 58 |
| T8 Three requests, two robots | C1 | 100% | 100% | 4 | 4596 | 2.01e-04 | 10 |
| T8  | C2 | 100% | 100% | 4 | 4596 | 2.01e-04 | 10 |
| T8  | C3 | 100% | 100% | 0 | 4596 | 8.00e-07 | 0 |

**NFR-4** (C1's model calls per task at most 50% of C2's): C1 150 vs C2 520, ratio **0.288** (met).

Notes: T7's WAN bytes differ because only C1 has an edge model to filter what the robots saw (the PRD expects C1 to use the least bandwidth); C2 and C3 ship every photo's result. T6's cloud decisions are the same in C1 and C2 because the edge is not involved until the recall reaches it.

## Plots

![success](../results/e3/success.svg)
![llm_calls](../results/e3/llm_calls.svg)
![cost](../results/e3/cost.svg)
![wan_bytes](../results/e3/wan_bytes.svg)
![completion](../results/e3/completion.svg)
![tradeoff](../results/e3/tradeoff.svg)
