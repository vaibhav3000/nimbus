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
