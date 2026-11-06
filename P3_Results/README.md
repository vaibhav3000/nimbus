# Person 3 (cloud) evaluation results: what was run, what it shows, what it does not

Everything in this folder was produced by **one command** on the latest `main`:

```bash
python scripts/run_p3_claims.py --out P3_Results      # about 3 minutes; it runs the existing experiment tools too
```

## Read this first: what "stand-in model" means

No AWS credentials exist in the environment these were run in, so **no number below comes from Amazon Bedrock**. The cloud's language model
is replaced by the rule-based stand-in (`cloud/llm.py: RuleBasedLLM`) in every run. That is the right tool for what the cloud code does *around*
the model (scheduling, rule checker, circuit breaker, fallback, leases, shared map, fan-out, replay, determinism, scale). It is **not** a measure
of how well a real model understands requests. Where a claim needs the real model, the row says **NEEDS BEDROCK** and gives the command.

The E2 label sets (`evaluation/e2/*.json`) are **DRAFT** (template-written, not reviewed by a person), and the stand-in parses the same templates,
so its 100 % is expected and only proves the scoring pipeline works.

## Results against PRD v6 Table 4 (cloud / shared rows)

| PRD | Acceptance criterion | Result here | Evidence | Verdict |
|---|---|---|---|---|
| **FR-8** | repeated, re-ordered, outdated messages: no step executed twice | repeat: 5 sent, 1 acted on, 4 acknowledged as duplicates; outdated (after a re-plan to v2): escalation and check-in both 409, state unchanged; re-ordered sets end in the same state | `e6_injection.txt` (exit 0) | **MEASURED** |
| **FR-9** | all escalations follow the format | escalation schema enforced by the cloud (400 with the violated paths otherwise); the three types validated live in the edge↔cloud audit (`docs/EDGE_CLOUD_PROTOCOL.md`); unit tests below | `pytest` evidence below | **MEASURED (schema + live capture)** |
| **FR-11** | every affected cluster informed, few unaffected | 11 closures (every zone class: east, west, shared spine): affected clusters informed **11 / 11**, unaffected clusters informed **0**; the map showed each closure in ≤ 0.04 s | `e6_sharing.csv` | **MEASURED** (ground truth = P1's wing map + live leases) |
| **FR-13** | ≥ 85 % of details right, vague ones get a question | stand-in: 100 % fields, clarification recall 100 %, precision 100 % (n = 50) | `e2_cloud.csv` | **NEEDS BEDROCK** (labels also DRAFT) |
| **FR-14** | urgent never waits behind a routine task | urgent request queued *after* a routine one got the freed robot first in **20 / 20** trials | `fr_checks.csv`, `e3/log_excerpts.md` §3 | **MEASURED** |
| **FR-15** | no invalid lease; after 2 failed proposals the rule-based assigner is used | 0 of 50 issued leases fail the lease schema; a model that proposes a non-existent robot every time is vetoed **twice**, then the deterministic assigner issues a valid lease | `fr_checks.csv`, `e3/log_excerpts.md` §1–2 | **MEASURED** |
| **FR-16** | new plan within ≈ 30 s on a good network | blocked zone → valid lease v2 in **12 / 12** re-plans, cloud processing p95 0.02 s (stand-in, no WAN, no model latency) | `fr_checks.csv`, `e3/log_excerpts.md` §4 | **PARTIAL**: cloud compute only; real model + WAN need AWS |
| **FR-17** | map changes reflected ≤ 5 min in ≥ 80 %; 10 test rules applied | map reflection **11 / 11**, max 0.04 s; rules **10 / 10** (stand-in) | `e6_sharing.csv`, `e2_cloud.csv` | map part **MEASURED**; rules **NEEDS BEDROCK** |
| **FR-18** | model forced to fail 10 min: leased tasks continue, new requests still leased by the deterministic path, no lease from the model path, user told reduced autonomy | during the 10-minute outage: **10 / 10** requests leased, **0** model calls made (the breaker skipped it), **0** leases from the model path, **10 / 10** requesters told "Reduced autonomy", a lease issued before the outage was renewed throughout, model used again after recovery | `e5_bedrock_down.csv` | **MEASURED** (outage injected with the repo's `FaultyLLM`; real Bedrock throttling not exercised) |
| **FR-19** | most context-dependent requests resolved without asking | stand-in: 20 / 20 (habit 10/10, control 5/5, override 5/5) | `e2_cloud.csv` | **NEEDS BEDROCK** (labels DRAFT) |
| **FR-20** | any task's timeline can be replayed | 3 tasks (clarification+lease+completion, re-plan, operator cancel): state rebuilt **only** from the decision-log rows matches the real task in **3 / 3**; every row has who, inputs, output, lease version; timestamps in order | `e6_replay.json` | **MEASURED** |
| **FR-22** (cloud half) | a cancel reaches the robots within seconds | cloud publishes the stop lease in p95 0.02 s (12 / 12). Edge pick-up was measured live earlier: stop within ~1 s (lease watcher) | `fr_checks.csv` | **PARTIAL**: edge + network half in P2's results |
| **NFR-1** (cloud half) | a request becomes a lease within 30 s (p95) | 34 leased of 50 requests (the rest correctly asked a question): p95 **0.017 s** wall time | `nfr1_request_to_lease.csv` | **NEEDS BEDROCK**: excludes model latency and WAN |
| **NFR-3** (cloud half) | p95 decision time at 10 robots ≤ 1.5× the 3-robot baseline | **1.04×** (also 1.05× at 6, 1.07× at 20); 1029 requests over 24 simulated hours; 0 deadlines missed; replay identical at every scale | `e7/` | **MEASURED** (cloud compute; edge half is P2's) |
| **NFR-4** | C1's model calls per task ≤ 50 % of C2's | `hospital_sim` map: **150 vs 520 calls = 0.288** (matches PRD v6's 28.8 %); six-robot `hospital_sim6` map: 180 vs 550 = 0.327 | `e3/`, `e3_sim6/` (240 runs each) | **MEASURED for call counts**; the cost-per-task figure uses a price table, not a CloudWatch/Cost Explorer export (**NEEDS AWS**) |
| **NFR-5** | least-privilege IAM, no secrets in code; synthetic data only | IAM/secrets audit **PASS** (2 justified wildcard exceptions listed); data-manifest audit **PASS** | `nfr5_audit_iam.txt`, `nfr5_audit_data.txt` | **MEASURED** (static audit, as the PRD specifies) |
| **NFR-6** | seeded re-run gives byte-identical decision logs | seeds 7, 11, 23: two runs with different hash seeds, logs **IDENTICAL** (24, 13, 14 rows) | `e6_seed_diff.txt` | **MEASURED** |
| **E3** (T1–T8, C1/C2/C3) | all tasks complete under all configs | `hospital_sim`: 240 runs, **100 %** success and deadlines met in every cell; C1 lowest WAN bytes on T7. `hospital_sim6`: every cell 100 % **except T8 (0 / 30)**, because T8 ("three requests, two robots": expects 2 leased + 1 queued) is written for a 2-robot cluster and sim6 has 3 robots per cluster, so all three are leased at once. That is a stale fixture, not a scheduling bug, but T8 needs a 3-request/3-robot variant for sim6 | `e3/`, `e3_sim6/` | **MEASURED**; completion time and latency are *modelled* constants, as the report states |

Unit/integration tests run for this folder (not the full suite): `tests/test_contracts.py test_e5_e6.py test_e3_harness.py test_e7_scale.py
test_ms2_sc3.py test_ms2_scenarios.py test_ms2_handover.py test_ms2_standdown.py` → **73 passed**.

## What still has to be run by the team (needs AWS)

```bash
# record real Bedrock calls once, then replay them for free and repeatable
export NIMBUS_LLM=bedrock NIMBUS_BEDROCK_MODEL=apac.amazon.nova-lite-v1:0 AWS_REGION=ap-south-1
NIMBUS_CASSETTE=data/e2.cassette.json NIMBUS_CASSETTE_MODE=record python scripts/score_e2.py --config C1      # FR-13, FR-17, FR-19
NIMBUS_CASSETTE=data/e3.cassette.json NIMBUS_CASSETTE_MODE=record python scripts/run_e3.py --seeds 1        # E3 with the real model
curl -s https://<api>/metrics | python -m json.tool          # request_to_lease_s p95 = NFR-1 (needs a few dozen real requests)
```
NFR-4's cost per task: export CloudWatch / Cost Explorer for a fixed 1-hour window per configuration (PRD §5).
FR-18 on real Bedrock: `curl -X POST https://<api>/llm/outage -d '{"seconds":600}'` (needs `NIMBUS_DEMO_FAULTS=1`), then send requests.

## Discrepancies found while doing this (please read)

1. **`results/e3/log_excerpts.md` looks hand-written.** PRD v6's Table 4 cites it for FR-9, FR-14, FR-15 and FR-16. Its entries are log lines
   (`"component": "cloud.rule_checker"`, `LEASE_VALIDATION_PASSED`, `invalid_leases_issued_cumulative`, …) that **no code in this repository emits**
   (searched), with a different lease format than the real one. I did **not** copy it. `P3_Results/e3/log_excerpts.md` replaces it with
   real decision-log rows from fresh runs. I recommend the PRD cite the new file and the old one be removed or marked illustrative. I did not change `results/`.
2. **NFR-4 depends on the map.** PRD v6's 28.8 % is the `hospital_sim` result (re-measured today: 150 vs 520). On `hospital_sim6`, which is now the default pack, it is 0.327. Both meet ≤ 50 %, but the PRD should say which map. Related: E3's T8 only holds on the 2-robot map (see the E3 row).
3. **FR-22 "urgent preemption in 1.12 s in T8"** (PRD v6 §3): E3's timings are modelled constants, so I could not find a measured source for 1.12 s.
   What is measured: the cloud publishes a stop lease in ~0.02 s and the edge acted within ~1 s of a cancel in the live stand-in run.
4. **A real-model weakness the stand-in cannot show:** with Bedrock (Nova Lite) a message like "hello who are you" produced a made-up delivery
   (an invented paediatric-pump task), because the model must call a tool and the cloud only checks that the item and zone exist. The stand-in asks a
   question for the same text. Suggested guard (not implemented): reject a submitted task unless the request text names a known item or place.
5. P1's single-corridor six-robot map has no alternative route to most wards, so a closed corridor ends in an operator alert, not a new lease. The re-plan
   trials (FR-16) and the outdated-message test (FR-8) therefore use the `hospital` pack, which has an alternative route. This is stated in each output file.

## Files

| File | Covers |
|---|---|
| `e2_cloud.csv` | FR-13, FR-17 (rules), FR-19, C1 and C3 (stand-in) |
| `e3/` | E3 on `hospital_sim` (the map E3 was written for): csv, json, plots, `e3-results.md`, run log, real log excerpts |
| `e3_sim6/` | the same E3 on the six-robot `hospital_sim6` map (T8 fixture mismatch, see above) |
| `e5_bedrock_down.csv` | FR-18 |
| `e6_injection.txt` | FR-8 |
| `e6_sharing.csv` | FR-11, FR-17 (map) |
| `e6_replay.json` | FR-20 |
| `e6_seed_diff.txt` | NFR-6 |
| `e7/` | NFR-3 (cloud half), NFR-4 per scale |
| `fr_checks.csv` | FR-14, FR-15, FR-16, FR-22 |
| `nfr1_request_to_lease.csv` | NFR-1 (cloud half) |
| `nfr5_audit_iam.txt`, `nfr5_audit_data.txt` | NFR-5 |
| `claims_summary.json` | every number quoted above |
