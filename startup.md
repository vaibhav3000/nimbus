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
- Plan on P1's Gazebo map instead of the built-in hospital: `NIMBUS_PACK=hospital_sim python server.py`
  (rebuild its topology after P1 updates their map: `python scripts/build_sim_topology.py`).
- Set stock: `curl -X POST localhost:8000/fleet/stock -H 'Content-Type: application/json' -d '{"cluster_id":"east-wing","stock":{"wheelchair":5}}'`

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
- **Clean slate:** stop everything and delete `data/`.
