# Nimbus: quick start

## Prerequisites
- Python 3.11+ (tested on 3.13). No other system dependencies for the offline mode.
- Optional, for the AWS mode: an AWS profile with `infra/iam/p3-dev-policy.json` and Bedrock model access.

## Setup
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt                 # add -r requirements-aws.txt for the AWS mode
```

## Start (offline, free)
```bash
python server.py                                # cloud API + requester app + operator console
python scripts/edge_sim.py --cluster east-wing --complete     # a fake edge (second terminal; add west-wing in a third)
python edge/run_supervisor.py --cluster east-wing             # or P2's real edge supervisor
```
- URL: http://127.0.0.1:8000 (requester app), http://127.0.0.1:8000/operator (console, includes stock by cluster).
- Change the port with `PORT=8010`, the data folder with `DATA_DIR=/tmp/nimbus`.
- The default map is P1's Gazebo topology (pack `hospital_sim`; rebuild it after P1 updates their map: `python scripts/build_sim_topology.py`).
  The older hand-made map is `NIMBUS_PACK=hospital`.
  For P1's six-racecar world with P2's edge (robots R1-R6), use `NIMBUS_PACK=hospital_sim6`.
- Set stock: `curl -X POST localhost:8000/fleet/stock -H 'Content-Type: application/json' -d '{"cluster_id":"east-wing","stock":{"wheelchair":5}}'`

## Evaluation configurations (C1 / C2 / C3)
```bash
NIMBUS_MODE=C1 python server.py     # proposed: cloud LLM + leases + edge decides locally (default)
NIMBUS_MODE=C2 python server.py     # cloud-only: the edge relay asks POST /relay/decide before every step
NIMBUS_MODE=C3 python server.py     # no LLM: forms, rules, classical allocator
python scripts/run_e3.py --seeds 10 # E3: T1-T8 under all three, writes results/e3/ (json, csv, 3 SVG plots)
```
`/relay/decide` body: `{cluster_id, lease_id, lease_version, step_index, message_id, cmd?, zone?, target?, blocked_zones?, attempt?}`
-> `{action: proceed|wait|reroute|abort, via_zones, source: llm|rules}`. It returns 409 unless the service runs C2.
E3 uses the offline stand-in model by default (token counts and latency are estimates); see the script header for a Bedrock run.

## See it on a map, and follow what is happening
Open **http://127.0.0.1:8000/map** while the server runs.

- **Left:** P1's building at readable size: closed zones (red ✕), restricted places (amber), live lease routes (thick; dashed = stop lease), robots beside their zone, spare-equipment counts.
- **Right:** send requests (scenario buttons), **answer the cloud's questions in the page** ("Needs your answer"), tasks with status / version / leg, and for the selected task:
  - **Conversation:** every message in order with the time, every model call (expand it to see what was asked and answered), every decision and lease.
  - **Replay:** step or play the same events with a scrubber; each step highlights its zones and route on the map. "Re-run this input" sends the same text again.
- Tick **"robots act on their leases"** and robots drive their routes at the lease speed x the speed slider, check in, and finish. Without it the page only watches; use a real or fake edge instead.
- Links: `/map#task-000003` (select a task), `/map#task-000003/replay`, `/map#auto&speed=20`.

From a terminal (same data): `python scripts/trace.py --last --detail` (add `--follow` to watch live; `--all` for everything).
`curl localhost:8000/tasks/task-000001/trace` and `curl localhost:8000/map.json` are the raw forms.

Realistic timing from the command line (a scripted fake edge, not P2's logic): `python scripts/edge_sim.py --cluster east-wing --pace 10 --complete`
drives each lease at lease speed x 10, checking in and reporting its position; add `--block east_mid_hall` to have the corridor
reported blocked half way along. Run one per wing for hand-overs.

## MS2 behaviour to try (offline)
```bash
python server.py                                     # plans on P1's Gazebo map (hospital_sim is now the default)
NIMBUS_PACK=hospital python server.py                # the older hand-made map (wards 2A, 2B, 2C, 3A, ward-5)
python scripts/edge_sim.py --cluster east-wing --block east_mid_hall --robot-zone east_waiting_approach   # SC-2 corridor
python scripts/edge_sim.py --cluster west-wing --close west_north_gate --robot-zone north_hall            # SC-2 gate
curl -X POST localhost:8000/requests -H 'Content-Type: application/json' \
  -d '{"text":"Ward 2C is now isolation. Get the spare pumps out, hold routine deliveries there, urgent ones can still go in.","message_id":"sc3-1"}'
curl 'localhost:8000/notifications?audience=operator'      # also audience=place|cluster|requester
```
- A request across wings is split into legs (`legs`, `leg_index` on `GET /tasks/{id}`); the next leg starts on OUTPUT_READY.
- Cancel and pause publish a stop lease (a lease whose only step is `stop`). Contract for the edge: `docs/p2-edge-contract-ms2.md`.
- Hand-offs to P4: `docs/p4-note-ms2.md`.

## Evaluation and verification commands
```bash
python scripts/run_e3.py --seeds 10 && python scripts/e3_report.py        # E3: C1/C2/C3 on T1-T8, writes results/e3 and docs/e3-results.md
python scripts/score_e2.py --config C1                                      # E2 on the DRAFT sets in evaluation/e2 (review the labels first)
python scripts/run_e7.py --robots 3,10,20 --hours 24                        # E7: scale and load on the generated 3-cluster pack
python scripts/replay_check.py --seed 7                                     # E6/NFR-6: two seeded runs, byte-identical decision logs
python scripts/inject_messages.py --api http://127.0.0.1:8000               # E6: repeated / outdated / re-ordered messages
curl -X POST localhost:8000/llm/outage -d '{"seconds":600}'                 # E5: the model is "down" for 10 minutes (0 ends it)
python scripts/audit_iam.py && python scripts/audit_data.py                 # NFR-5: IAM/secrets audit, synthetic-data check
python scripts/aws_verify.py                                                # your checks against real AWS (docs/aws-verify.md)
curl localhost:8000/metrics                                                 # request_to_lease_s p95 is the NFR-1 measurement
```

## Environment variables the cloud reads
| Variable | Effect |
|---|---|
| `NIMBUS_PACK` | `hospital_sim` (default, P1's map), `hospital` (hand-made), `rescue`, or a path |
| `NIMBUS_MODE` | `C1` (default), `C2` (cloud-only relay), `C3` (no model) |
| `NIMBUS_BACKEND`, `NIMBUS_LLM`, `NIMBUS_BEDROCK_MODEL`, `AWS_REGION`, `NIMBUS_DDB_PREFIX` | AWS mode (see below) |
| `NIMBUS_CASSETTE`, `NIMBUS_CASSETTE_MODE` | record or replay model calls |
| `NIMBUS_RESULTS_BUCKET` | S3 bucket for result previews, task archives (`logs/`) and bundles (`bundles/`); without it a local `results/` folder is used |
| `NIMBUS_SQS_QUEUE_URL` | consume edge messages from this queue as well as over HTTP/MQTT |
| `NIMBUS_KB_ID` | Bedrock Knowledge Base for rule retrieval (keywords from the pack otherwise) |
| `NIMBUS_EDGE_TOKEN` | require `Authorization: Bearer` on edge routes |
| `NIMBUS_TICK_S` | ticker period in seconds (default 5; 0 turns the ticker off) |
| `NIMBUS_EMF`, `NIMBUS_EMF_EVERY_S` | print CloudWatch EMF metric lines (every 60 s by default) |
| `NIMBUS_BUNDLES=0` | do not send lease bundles |
| `NIMBUS_LLM_OUTAGE=<after_s>:<duration_s>` | schedule a model outage (E5) |

## AWS mode (Bedrock + DynamoDB)
```bash
export AWS_PROFILE=<profile> AWS_REGION=ap-south-1
export NIMBUS_BACKEND=aws NIMBUS_LLM=bedrock NIMBUS_BEDROCK_MODEL=apac.amazon.nova-lite-v1:0
python scripts/smoke_live.py --model $NIMBUS_BEDROCK_MODEL --teardown    # one live check; creates and deletes its own tables
```
`server.py` needs its DynamoDB tables to exist (see `context/p3-cloud.md`). Stock counts then survive restarts.

## Stop and test
- Stop with Ctrl-C in each terminal.
- Tests: `python -m pytest -q` (all offline). `NIMBUS_BACKEND=aws python -m pytest -q` runs the same suite on an emulated DynamoDB.

## Common problems
- **Address already in use:** another server holds port 8000. Stop it, or use `PORT=8010`.
- **Edge shows `DROPPED_STALE`:** it already ran that lease version (duplicate protection). Use a fresh `--db` file for a fresh start.
- **Tasks stay `queued`:** no free robot or no stock in the cluster that covers the destination. Check `/fleet`, or restock.
- **`/relay/decide` returns 409:** the service is not in C2. Restart with `NIMBUS_MODE=C2`.
- **Cancel/pause returns 409:** the task is already completed, cancelled or rejected.
- **A task says `queued: ... hold by SOP-ISO-02`:** a restriction is active on its place. Send "Ward X is no longer in isolation" or mark it urgent.
- **`replan_failed` / operator alert on a hand-over:** no route is left (both gates closed) or the robot's zone is not a map node.
- **Clean slate:** stop everything and delete `data/`.
