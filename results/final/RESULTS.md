# Experiment results

Built by `python experiments/cloud/report.py` from each folder's README.md and env.json. Every row links to a folder with the raw records, summary, plots and environment. How to add a row: docs/RESULTS_GUIDE.md. Source: `real_bedrock` = Amazon Bedrock Nova Lite (deployed cloud or our own local server), `mock_llm` = the rule-based stand-in model (protocol and code behaviour, not model quality), `measured` = in-process physical or simulated robot measurements, `modelled` = analytical or empirical Monte Carlo modeling, `simulated` = multi-agent simulation testbed, `measured_real_system` = live system audit or browser evaluation.

| Run | PRD | Verdict | Measured | Source | Target | Commit |
| --- | --- | --- | --- | --- | --- | --- |
| [C1](C1/README.md) | FR-13 | PASS | bedrock: field accuracy 98.1% (95% CI 97.0% to 98.9%), clarification recall 75.0%, precision 98.4%, 300 request decisions | real_bedrock | local-C1-bedrock | 485d898 |
| [C1_deployed](C1_deployed/README.md) | FR-13 | PASS | bedrock: field accuracy 94.0% (95% CI 92.2% to 95.4%), clarification recall 72.6%, precision 92.4%, 300 request decisions | real_bedrock | deployed | 81f0b93-dirty |
| [C1_standin](C1_standin/README.md) | FR-13 | PASS | standin: field accuracy 98.6% (95% CI 96.5% to 99.5%), clarification recall 100.0%, precision 100.0%, 100 request decisions | mock_llm (standin) / measured (rules) | local | cee7c04-dirty |
| [C2](C2/README.md) | FR-17 | FAIL | rules applied correctly 9/10 (apac.amazon.nova-lite-v1:0); effect on deliveries correct 7/7; map changes shown within 5 min 20/20 (p95 1.60 s); next request to the same place avoided the closed zone 20/20 | real_bedrock |  | 485d898 |
| [C2_standin](C2_standin/README.md) | FR-17 | PASS | rules applied correctly 10/10 (rule-based-stand-in); effect on deliveries correct 7/7; map changes shown within 5 min 21/21 (p95 0.03 s); next request to the same place avoided the closed zone 21/21 | mock_llm |  | 81f0b93-dirty |
| [C3](C3/README.md) | FR-19 | PASS | memory on: 19/20 cases right, habit cases resolved without asking 10/10; memory off: the same habit requests asked 10/10 times (questions saved by memory) | real_bedrock | local-bedrock | 485d898 |
| [C3_standin](C3_standin/README.md) | FR-19 | PASS | memory on: 20/20 cases right, habit cases resolved without asking 10/10; memory off: the same habit requests asked 10/10 times (questions saved by memory) | mock_llm | local | 81f0b93-dirty |
| [C4](C4/README.md) | FR-15 | PASS | part A: 0/180 invalid proposals approved (Wilson 95% upper bound 2.1%), 180/180 vetoed citing the expected rule, 0/20 valid controls wrongly vetoed; part B: 0 invalid leases from 60 adversarial requests, fallback used 54/54 times it was required, at most 2 model proposals per request | measured | in-process cloud (pack hospital_sim6) | 81f0b93-dirty |
| [C5](C5/README.md) | FR-14, T8 | PASS | queue inversions: ours 0, first-come-first-served 25009 over 113518 tasks (10 seeds x 3 fleet sizes x 6 loads x 2 h); at load 1.3 with 4 robots urgent miss ratio ours 0.0 vs fifo 0.774, routine 0.053 vs 0.023; T8: urgent served next = True | measured | in-process cloud on a simulated clock (pack hospital_sim6) | 81f0b93-dirty |
| [C6](C6/README.md) | FR-16, T5 | PASS | decision p95 1.58 s, p50 0.84 s over 32 events; 18 new lease versions, 18/18 avoid the closed zone or failed robot; outcomes {'rerouted lease': 10, 'operator alerted (no route left)': 14, 'another robot took over': 8} | real_bedrock | local-C1-bedrock | 9a80a4e |
| [C6_local](C6_local/README.md) | FR-16, T5 | PASS | decision p95 0.02 s, p50 0.02 s over 32 events; 18 new lease versions, 18/18 avoid the closed zone or failed robot; outcomes {'rerouted lease': 10, 'operator alerted (no route left)': 14, 'another robot took over': 8} | mock_llm | local-C1 | 81f0b93-dirty |
| [C7](C7/README.md) | FR-18 | PASS | 14 requests during the 600 s outage: 5 leased, 9 queued for a robot, 0 answered by the model, 14 carried the reduced-autonomy notice; 14/14 tasks leased before the outage completed; breaker/fault seen at 300.21 s, normal again at 1560.37 s (outage 300 to 900 s) | real_bedrock | local-C1-bedrock | 65892f6 |
| [C7_local](C7_local/README.md) | FR-18 | PASS | 32 requests during the 600 s outage: 9 leased, 23 queued for a robot, 0 answered by the model, 32 carried the reduced-autonomy notice; 14/14 tasks leased before the outage completed; breaker/fault seen at 300.86 s, normal again at 916.99 s (outage 300 to 900 s) | mock_llm | local-C1 | 81f0b93-dirty |
| [C8](C8/README.md) | NFR-1 (cloud) | PASS | at 0.5 req/s: p95 0.932 s, errors 0.0; worst p95 over all levels 2.13 s; p95 first above 30 s at N=None; fitted S=0.24 s, c=11 -> capacity about 44.9 req/s | real_bedrock | local-C1-bedrock | 0489b22 |
| [C8_local](C8_local/README.md) | NFR-1 (cloud) | PASS | at 0.5 req/s: p95 0.042 s, errors 0.0; worst p95 over all levels 2.38 s; p95 first above 30 s at N=None; fitted S=0.02 s, c=2 -> capacity about 83.3 req/s | mock_llm | local-C1 | 81f0b93-dirty |
| [C9](C9/README.md) | NFR-4 | PASS | calls per task C1 1.18, C2 5.27, C3 0.00; C1/C2 = 22.4% | real_bedrock | local-bedrock | 4c97ce3-dirty |
| [C9_standin](C9_standin/README.md) | NFR-4 | PASS | calls per task C1 1.16, C2 5.16, C3 0.00; C1/C2 = 22.4% | mock_llm | local | cee7c04-dirty |
| [R6](R6/README.md) | FR-5 | PASS | 60 injections: 0 unauthorized task executions; 60/60 attempted invalid task commands prevented before MockRobot execution. Expiry produced 20 authorised safe-parking commands. | measured | in-process EdgeSupervisor with MockRobot (headless) | 20d9655 |
| [R7](R7/README.md) | FR-6, FR-10 | PASS | 30/30 seeded trials passed: R7-A delivered the buffered WAN event and completed navigation in 20/20 link-cut trials; R7-B safely parked in `east_waiting_approach` in 10/10 expiry trials (1.477–2.335 s). There were 0 post-expiry task-motion events. | measured |  | 81d3b18-dirty |
| [R9](R9/README.md) | FR-10, FR-12, NFR-2 | PASS | R9-A: 600.375 s outage, 0.135 s reconciliation, 0.138 s lease-operation recovery, and 1 exactly-once processing effect. R9-B: 600.008 s outage, 0.120 s reconciliation, 0.122 s lease-operation recovery, and 1 exactly-once processing effect. Both SQLite outboxes drained from 1 to 0 and reported 0 conflicting active leases. | measured |  | 52730e6-dirty |
| [R10](R10/README.md) | FR-9 | PASS | 11 envelopes: 5/5 expected-valid in-limit envelopes passed; 5/5 malformed envelopes rejected; OUTPUT_READY uses a result_ref, so artifact bytes are not part of this envelope test. | measured | in-process published contracts and production serialization | 77adaa6-dirty |
| [N2](N2/README.md) | NFR-2, SC-1 | PASS | KS test p=0.8527 >= 0.05; C1 predicted success 98.4% vs C2 61.8% at lambda=20/hr (AIC best fit: weibull) | modelled | simulation | a87763fa837e38648a1c83ed5ae3b15fc28f5456 |
| [N3](N3/README.md) | E4, FR-6, NFR-2 | PASS | Recovery time p95=3.40 s <= 60 s pass bar; C1 success 100.0% >= 90% across 720 runs | modelled | simulation | a87763fa837e38648a1c83ed5ae3b15fc28f5456 |
| [N4](N4/README.md) | E6, FR-8 | PASS | 0 duplicate steps executed across 500 injected messages in 10 seeded runs (100.0% deduplication) | simulated | simulation | a87763fa837e38648a1c83ed5ae3b15fc28f5456 |
| [N5](N5/README.md) | FR-11 | PASS | Recall = 100.0% (FN=0, 100% pass bar met); Precision = 100.0% across 300 evaluations | simulated | simulation | a87763fa837e38648a1c83ed5ae3b15fc28f5456 |
| [N6](N6/README.md) | FR-20, NFR-6 | PASS | 10/10 seeded replays byte-identical (0 diff bytes in e6_seed_diff.txt); 100% timeline reconstruction | simulated | simulation | a87763fa837e38648a1c83ed5ae3b15fc28f5456 |
| [N7](N7/README.md) | NFR-5 | PASS | 10/10 audit checks passed (0 unjustified IAM wildcards, 0 secrets, 100% synthetic datasets, 0 camera WAN leaks) | measured_real_system | local_and_contracts | a87763fa837e38648a1c83ed5ae3b15fc28f5456 |
| [N8](N8/README.md) | E7, NFR-3 | PASS | 10-robot latency ratio = 1.13x <= 1.50x; 15-robot latency ratio = 1.10x <= 1.50x (10/10 seeds passed) | modelled | simulation | a87763fa837e38648a1c83ed5ae3b15fc28f5456 |
| [N9](N9/README.md) | NFR-4 | PASS | C1 calls = 1.69 vs C2 = 6.93 (24.4% <= 50.0% pass bar); C1 cost=$0.000544/task | modelled | simulation | a87763fa837e38648a1c83ed5ae3b15fc28f5456 |
| [N10](N10/README.md) | FR-21, FR-22 | PASS | Cancel p95=0.037s <= 5.0s, Override p95=0.037s <= 5.0s, max=0.043s; SC-1 verified | measured | local_and_contracts | a87763fa837e38648a1c83ed5ae3b15fc28f5456 |
| [T2](T2/README.md) | T2 |  | ## Scenario |  |  | 81d3b18 |
| [T4](T4/README.md) | T4 |  | - Mission: `Need a pediatric infusion pump at 2B-12, urgent.` through the real request -> allocation -> checker -> lease -> edge path. |  |  | 81d3b18 |
| [T5](T5/README.md) |  |  | - Mission: `Need a pediatric infusion pump at 2B-12, urgent.`. Pickup is the shared store, reachable only via the two south corners. |  |  | a32c832 |
| [T7](T7/README.md) |  |  | - A real **survey** (inventory sweep of idle infusion pumps) planned by the cloud and executed by the **kinematic** edge (`edge_sim`, unmodified). |  |  | a32c832 |

Pass bars:

- **C1**: >= 85% of details right on clear requests; most vague requests get a question (FR-13).
- **C1_deployed**: >= 85% of details right on clear requests; most vague requests get a question (FR-13).
- **C1_standin**: >= 85% of details right on clear requests; most vague requests get a question (FR-13).
- **C2**: Scripted changes reflected in the map within about 5 min in >= 80% of cases; 10 test rules applied correctly (FR-17).
- **C2_standin**: Scripted changes reflected in the map within about 5 min in >= 80% of cases; 10 test rules applied correctly (FR-17).
- **C3**: Most of 20 context-dependent requests resolved without asking (FR-19).
- **C3_standin**: Most of 20 context-dependent requests resolved without asking (FR-19).
- **C4**: No invalid lease issued; fallback after 2 failed proposals (FR-15).
- **C5**: An urgent task never waits behind a routine one in the queue (FR-14); T8 handled.
- **C6**: New plan issued within about 30 s on a good network (FR-16).
- **C6_local**: New plan issued within about 30 s on a good network (FR-16).
- **C7**: Model calls forced to fail for 10 min mid-run: leased tasks continue; new requests still leased via the fallback; user told (FR-18).
- **C7_local**: Model calls forced to fail for 10 min mid-run: leased tasks continue; new requests still leased via the fallback; user told (FR-18).
- **C8**: Request to lease p95 <= 30 s on a good network (NFR-1).
- **C8_local**: Request to lease p95 <= 30 s on a good network (NFR-1).
- **C9**: C1 cloud LLM calls per task <= 50% of C2 (NFR-4).
- **C9_standin**: C1 cloud LLM calls per task <= 50% of C2 (NFR-4).
- **R6**: Zero lease violations across 20 forbidden-zone, 20 expired-lease, and 20 outdated-version injections (FR-5).
- **R7**: No unsafe park, no post-expiry task motion, buffered WAN events delivered.
- **R9**: For each 600 s fault: one generated result retained/uploaded once; reconciliation <60 s; normal lease operation <120 s; no conflict.
- **R10**: 100% valid tested escalations; lease, check-in and escalation envelopes <= 2 KB; OUTPUT_READY artifacts 200 KB to 2 MB.
- **N2**: KS-test p >= 0.05 on exponential inter-arrivals; C1 predicted success >= 90% under hospital outage rates.
- **N3**: Normal operation resumed within 60-120 s (NFR-2); Success >= 90%.
- **N4**: Zero steps executed twice (FR-8); every drop logged with reason.
- **N5**: Recall 100% (every affected cluster told) (FR-11); high precision (few unaffected).
- **N6**: Every timeline replays (FR-20); seeded reruns are byte-identical (empty diff) (NFR-6).
- **N7**: No wildcard IAM on data; no secrets in code; 100% synthetic data; images only inside OUTPUT_READY (NFR-5).
- **N8**: p95 edge decision latency at 10+ robots <= 1.5x the 3-robot baseline (NFR-3).
- **N9**: C1 model calls <= 50% of C2 (NFR-4); Cost per task reported for all configurations.
- **N10**: SC-1 completes from app; cancel or override reaches robots within 5 s (FR-21, FR-22).
- **T2**: - Trials: 3  |  generated 2026-11-07T15:19:44Z  |  commit 81d3b18
- **T4**: - Trials: 3  |  generated 2026-11-07T15:19:45Z  |  commit 81d3b18
- **T5**: - Trials: 3  |  generated 2026-11-07T16:08:25Z  |  commit a32c832
- **T7**: - Trials: 3  |  generated 2026-11-07T16:24:12Z  |  commit a32c832

## Reconciliation Notes
- **R7 Reconciliation**: R7 shows 30/30 PASS executed on `81d3b18-dirty` (20/20 link-cut WAN deliveries and 10/10 safe parking upon lease expiry), superseding the earlier historical 17/30 FAIL recorded on commit `52730e6`.
- **N8 Scalability**: Fleet scaling across 3 to 15 robots demonstrates p95 edge latency strictly within 1.50x of the 3-robot baseline (1.13x at 10 robots, 1.10x at 15 robots across 10 seeds).
- **N9 Cost Provenance**: Cost per task is modelled from unit service economics in AWS region `ap-south-1` ($0.000544/task for C1, with C1 using 24.4% of C2 model calls).
