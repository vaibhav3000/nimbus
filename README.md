# Nimbus

LLM-orchestrated multi-robot fleets with leased authority across cloud and edge.
DS252 (IISc), Team Nimbus. Built against PRD v5.0.

A staff member types a request in plain language, for example "Need an infusion pump at 2B-12, urgent".
The **cloud** turns it into a task, asks a question when something is unclear, plans which robots
should do it, checks the plan against the rules, and issues a **lease**. A lease is time-, zone- and
scope-bounded authority for an **edge** cluster to carry out the task on its own. The edge keeps working
through network drops, checks in to renew the lease, and escalates only when it must
(`CLARIFY`, `UNEXPECTED_EVENT`, `OUTPUT_READY`). Every decision is logged and can be replayed.

```
 requester app ──HTTP──►  cloud                                   edge cluster          robots (simulated)
 operator console         API · LLM (Bedrock) · rule checker ──lease──► supervisor + SLM ──► navigation + safety stop
                          leases · decision log · cost counters ◄─check-in / escalation──
```

## Components

| Component | What it does | Where | Owner |
|---|---|---|---|
| Wire contracts | Lease, escalation, robot sub-task and decision-log schemas; messaging topics | `contracts/`, `contracts.py` | P4 |
| API and apps | HTTP API, requester app (FR-21), operator console (FR-22) | `server.py`, `static/` | P4 |
| Decision log and cost | Append-only, replayable log (FR-20); PRD Sec. 7 cost counters | `store.py`, `cost.py` | P4 |
| Cloud decision plane | Request understanding, robot allocation, rule checker, lease manager, re-planning, fallback | `cloud/`, `packs/` | P3 |
| Infrastructure | AWS deployment (Terraform), container image | `infra/`, `Dockerfile` | P4 (Terraform), P3 (image, decision-plane resources) |
| Edge cluster | Runs leases locally, checks in, escalates | separate; integrates via the API and `contracts/` | P2 |
| Simulation and evaluation | Robots in Gazebo, building map, experiment replay | separate; map enters as `packs/hospital/topology.json` | P1 |

The contracts are the team's shared wire format. Change them only through a reviewed pull request.

## Quick start (no AWS account needed)

```bash
pip install -r requirements.txt
python server.py                                # http://127.0.0.1:8000, operator console at /operator
python scripts/edge_sim.py --once --complete    # optional, second terminal: a simulated edge cluster
python -m pytest -q                             # tests
```

Send "Need an infusion pump at 2B-12, urgent." in the app. It asks whether a paediatric pump is
needed. Your answer produces lease v1. The timeline replays every step: request, question, answer,
proposal, rule check, lease, check-in and result.

Locally, state lives in files under `data/` and the LLM is a deterministic rules-only stand-in, so a run
costs nothing and repeats exactly.

## Running with AWS services

The same code runs against Amazon Bedrock and DynamoDB when these are set (full list in `context/p3-cloud.md`):

```bash
pip install -r requirements-aws.txt
export AWS_PROFILE=<profile> AWS_REGION=ap-south-1
export NIMBUS_BACKEND=aws NIMBUS_LLM=bedrock NIMBUS_BEDROCK_MODEL=apac.amazon.nova-lite-v1:0
python server.py
```

`scripts/bedrock_hello.py` checks model access, and `scripts/smoke_live.py --teardown` runs one full
request on throwaway tables. `infra/iam/` has a least-privilege policy for a developer's own IAM user.

## Container and deployment

```bash
docker build -t nimbus-app .
docker run -p 8000:8000 -e NIMBUS_BACKEND=local -e NIMBUS_LLM=fake nimbus-app   # local check
```

In AWS the image runs on ECS Fargate and gets its permissions from an IAM task role, so it holds no keys.
The deployment is described in `infra/terraform/` (PRD Fig. 4):

```bash
cd infra/terraform && terraform init -backend=false && terraform validate
```

`terraform apply` is run only by the infrastructure owner, on the team account, after the team agrees.

## API

| Method + path | Purpose |
|---|---|
| `POST /requests` `{text, message_id}` | New request. Returns a clarifying question, a lease, or a queued or rejected status with the reason |
| `POST /requests/{id}/answer` `{text, message_id}` | Answer a clarifying question |
| `POST /escalations` | Edge escalation: `CLARIFY`, `UNEXPECTED_EVENT` (re-plan, lease v2), `OUTPUT_READY` (completes the task) |
| `POST /checkins` | Edge check-in. Renews the lease (same version, later `expiry_ts`) |
| `POST /fleet/heartbeat` | Cluster heartbeat with optional robot states and stock |
| `POST /tasks/{id}/control` | Operator pause or cancel |
| `POST /tasks/{id}/replan` | Operator-requested re-plan |
| `GET /tasks`, `GET /tasks/{id}`, `GET /tasks/{id}/timeline` | Task list, task detail, decision-log replay |
| `GET /fleet`, `GET /pack` | Cluster health and robots; the active domain pack |
| `GET /outbox` | Messages to the edge (leases, renewals, probes), standing in for IoT Core |
| `GET /schemas/{name}.json` | The published contracts |
| `GET /cost` | Cost counters |

Repeated `message_id`s are ignored, and stale lease versions are refused (FR-8). Cloud-edge topic names
are in `contracts/topics.md`.

## Decision log and cost counters

Each decision is one row (`requester`, `cloud`, `rule` or `edge`) with its inputs, output and lease
version, appended in order and replayed by `GET /tasks/{id}/timeline`.

`GET /cost` returns every term of the PRD Sec. 7 formula as raw counters:
`Cost = [T_in·p_in + T_out·p_out + F·p_fargate + M·p_iot + W·p_ddb + Q·p_sqs + S·p_s3 + D·p_transfer + G·p_apigw + K·p_cw] / N_tasks`.
Prices stay symbolic, as in the PRD.

## Repository layout

```
contracts/, contracts.py   wire contracts (JSON Schema) and validation
server.py                  HTTP API and static apps
static/                    requester app (/) and operator console (/operator)
store.py, cost.py          local task store and decision log; cost counters
standin.py                 the original rules-only stand-in (kept for reference and tests)
cloud/                     decision plane: context, LLM adapters, allocation, rule checker, leases, AWS backends
packs/                     domain data (hospital, rescue): map, fleet, rules, prompts, scenarios
scripts/                   simulated edge, live AWS checks
infra/terraform/, infra/iam/   AWS deployment and developer policy
tests/                     API, contract, decision-plane and emulated-DynamoDB tests
context/                   docs for contributors and coding agents
Dockerfile                 container image for the cloud service
```

## Documentation

* `context/README.md`: start here (architecture, decisions, status, file map, team)
* `context/p3-cloud.md`: decision plane, settings, AWS notes
* `SPEC-P4.md`, `ASSUMPTIONS.md`, `HANDOFF.md`, `REUSE.md`: foundation spec, PRD readings, hand-off, reuse record
* `GLOSSARY.md`: shared vocabulary

## Milestones

MS1 Sun 18 Oct (request to lease, T1 end to end) · MS2 Sun 1 Nov · MS3 Fri 6 Nov.
Current status: `context/status.yaml`.
