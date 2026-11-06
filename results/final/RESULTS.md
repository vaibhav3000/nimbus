# Experiment results

Built by `python experiments/cloud/report.py` from each folder's README.md and env.json. Every row links to a folder with the raw records, summary, plots and environment. How to add a row: docs/RESULTS_GUIDE.md. Source: `real_bedrock` = Amazon Bedrock Nova Lite (deployed cloud or our own local server), `mock_llm` = the rule-based stand-in model (protocol and code behaviour, not model quality), `measured` = no model involved (or a physical/simulated measurement).

| Run | PRD | Verdict | Measured | Source | Target | Commit |
| --- | --- | --- | --- | --- | --- | --- |
| [C1_deployed](C1_deployed/README.md) | FR-13 | PASS | bedrock: field accuracy 94.0% (95% CI 92.2% to 95.4%), clarification recall 72.6%, precision 92.4%, 300 request decisions | real_bedrock | deployed | 81f0b93-dirty |
| [C1_standin](C1_standin/README.md) | FR-13 | PASS | standin: field accuracy 98.6% (95% CI 96.5% to 99.5%), clarification recall 100.0%, precision 100.0%, 100 request decisions | mock_llm (standin) / measured (rules) | local | cee7c04-dirty |
| [C2_standin](C2_standin/README.md) | FR-17 | PASS | rules applied correctly 10/10 (rule-based-stand-in); effect on deliveries correct 7/7; map changes shown within 5 min 21/21 (p95 0.03 s); next request to the same place avoided the closed zone 21/21 | mock_llm |  | 81f0b93-dirty |
| [C3_standin](C3_standin/README.md) | FR-19 | PASS | memory on: 20/20 cases right, habit cases resolved without asking 10/10; memory off: the same habit requests asked 10/10 times (questions saved by memory) | mock_llm | local | 81f0b93-dirty |
| [C4](C4/README.md) | FR-15 | PASS | part A: 0/180 invalid proposals approved (Wilson 95% upper bound 2.1%), 180/180 vetoed citing the expected rule, 0/20 valid controls wrongly vetoed; part B: 0 invalid leases from 60 adversarial requests, fallback used 54/54 times it was required, at most 2 model proposals per request | measured | in-process cloud (pack hospital_sim6) | 81f0b93-dirty |
| [C5](C5/README.md) | FR-14, T8 | PASS | queue inversions: ours 0, first-come-first-served 25009 over 113518 tasks (10 seeds x 3 fleet sizes x 6 loads x 2 h); at load 1.3 with 4 robots urgent miss ratio ours 0.0 vs fifo 0.774, routine 0.053 vs 0.023; T8: urgent served next = True | measured | in-process cloud on a simulated clock (pack hospital_sim6) | 81f0b93-dirty |
| [C6_local](C6_local/README.md) | FR-16, T5 | PASS | decision p95 0.02 s, p50 0.02 s over 32 events; 18 new lease versions, 18/18 avoid the closed zone or failed robot; outcomes {'rerouted lease': 10, 'operator alerted (no route left)': 14, 'another robot took over': 8} | mock_llm | local-C1 | 81f0b93-dirty |
| [C7_local](C7_local/README.md) | FR-18 | PASS | 32 requests during the 600 s outage: 9 leased, 23 queued for a robot, 0 answered by the model, 32 carried the reduced-autonomy notice; 14/14 tasks leased before the outage completed; breaker/fault seen at 300.86 s, normal again at 916.99 s (outage 300 to 900 s) | mock_llm | local-C1 | 81f0b93-dirty |
| [C8_local](C8_local/README.md) | NFR-1 (cloud) | PASS | at 0.5 req/s: p95 0.042 s, errors 0.0; worst p95 over all levels 2.38 s; p95 first above 30 s at N=None; fitted S=0.02 s, c=2 -> capacity about 83.3 req/s | mock_llm | local-C1 | 81f0b93-dirty |
| [C9_standin](C9_standin/README.md) | NFR-4 | PASS | calls per task C1 1.16, C2 5.16, C3 0.00; C1/C2 = 22.4% | mock_llm | local | cee7c04-dirty |

Pass bars:

- **C1_deployed**: >= 85% of details right on clear requests; most vague requests get a question (FR-13).
- **C1_standin**: >= 85% of details right on clear requests; most vague requests get a question (FR-13).
- **C2_standin**: Scripted changes reflected in the map within about 5 min in >= 80% of cases; 10 test rules applied correctly (FR-17).
- **C3_standin**: Most of 20 context-dependent requests resolved without asking (FR-19).
- **C4**: No invalid lease issued; fallback after 2 failed proposals (FR-15).
- **C5**: An urgent task never waits behind a routine one in the queue (FR-14); T8 handled.
- **C6_local**: New plan issued within about 30 s on a good network (FR-16).
- **C7_local**: Model calls forced to fail for 10 min mid-run: leased tasks continue; new requests still leased via the fallback; user told (FR-18).
- **C8_local**: Request to lease p95 <= 30 s on a good network (NFR-1).
- **C9_standin**: C1 cloud LLM calls per task <= 50% of C2 (NFR-4).
