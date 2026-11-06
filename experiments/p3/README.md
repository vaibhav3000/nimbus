# P3 (cloud) experiments C1 to C9

Every experiment drives the cloud through its public HTTP API (or, where state must be set up directly, the real cloud
code in-process) and writes `results/final/<ID>/` with `raw/` (one line per trial), `summary.csv`, `plots/`, `env.json`
and a `README.md` with the claim, the PRD pass bar, the measured value and PASS/FAIL. Nothing is hard-coded: every
number is computed from the raw records of that run. `python experiments/p3/report.py` rebuilds
`results/final/P3_RESULTS.md` from all folders.

## Where the cloud runs (`--target`)

| Target | What it is | `source` label |
| --- | --- | --- |
| `local-bedrock` (default) | `server.py` started by the harness on a free port, own empty data folder, **our own Amazon Bedrock Nova Lite** | `real_bedrock` |
| `local` | same, with the rule-based stand-in model | `mock_llm` |
| `deployed` / URL | the Terraform deployment (API Gateway, container service, DynamoDB, S3, Bedrock); only C8 needs it | `real_bedrock` |

`local-bedrock` needs AWS credentials that can call `bedrock-runtime` Converse in `ap-south-1`
(`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and `AWS_SESSION_TOKEN` if temporary). Model: `NIMBUS_BEDROCK_MODEL`
(default `apac.amazon.nova-lite-v1:0`).

## The experiments

| ID | PRD | Script | Default run | What it does |
| --- | --- | --- | --- | --- |
| C1 | FR-13 | `c1_understanding.py` | 100 labelled requests (50 PRD + 50 held-out paraphrases) x 3 repeats on the real model, plus stand-in and rules-only | scores item, destination, priority, deadline and whether vague requests get a question |
| C2 | FR-17 | `c2_rules_map.py` | 10 plain-language rules (+ their effect on deliveries); 21 edge-reported closures | rules applied, time until the map shows a closure, next request routes around it |
| C3 | FR-19 | `c3_memory.py` | 20 context cases, memory on vs off (`/reset-memory`) | habits resolve repeat requests without asking; controls still ask |
| C4 | FR-15 | `c4_checker_attack.py` | 200 proposals to the Rule Checker; 60 requests with an adversarial model | zero invalid leases; fallback after two failed proposals |
| C5 | FR-14, T8 | `c5_scheduling.py` | real cloud on a simulated clock, Poisson arrivals, 2/4/6 robots, load 0.3 to 1.3, 10 seeds, ours vs FIFO | queue inversions, waiting time and deadline misses by priority |
| C6 | FR-16, T5 | `c6_replan.py` | 32 incidents: blocked with/without detour, closed door, robot fault | decision time, new lease version, plan avoids the zone or robot |
| C7 | FR-18 | `c7_bedrock_outage.py` | 20 min of requests, model down for 10 min via `/llm/outage` | leases continue from the rules, users told, breaker trip and recovery |
| C8 | NFR-1 | `c8_load.py` | 1 to 64 parallel clients and Poisson rates on the deployed cloud | latency, throughput, errors, saturation, M/M/c fit |
| C9 | NFR-4 | `c9_llm_calls.py` | T1-T5, T7, T8 under C1, C2, C3 with protocol-faithful simulated edges | model calls and tokens per task by step |

## Run everything

```bash
python experiments/p3/c4_checker_attack.py
```

```bash
python experiments/p3/c5_scheduling.py --seeds 10 --loads 0.3 0.5 0.7 0.9 1.1 1.3
```

```bash
python experiments/p3/c1_understanding.py --target local-bedrock
```

```bash
python experiments/p3/c2_rules_map.py --target local-bedrock --llm bedrock
```

```bash
python experiments/p3/c3_memory.py --target local-bedrock
```

```bash
python experiments/p3/c6_replan.py --target local-bedrock
```

```bash
python experiments/p3/c7_bedrock_outage.py --target local-bedrock
```

```bash
python experiments/p3/c9_llm_calls.py --target local-bedrock --repeats 5
```

```bash
python experiments/p3/c8_load.py --target deployed
```

```bash
python experiments/p3/report.py
```

C8 refuses to start while real edges are connected to the deployment (their robots would receive the load test's
leases); every experiment that uses the deployment cancels all tasks it created when it ends.
