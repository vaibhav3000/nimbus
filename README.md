# Nimbus - Person 4 slice (DS252, Team Nimbus)

The shared wire contracts, the requester app (FR-21), the decision log (FR-20),
a thin local API with a **cloud stand-in** so everything runs with no AWS
account, the Terraform skeleton for the real AWS deployment (PRD Fig 4), and
the cost counters for the PRD Sec. 7 formula.

Built against Nimbus PRD v5.0 (26 Sep 2026). Spec: SPEC-P4.md. Terms:
GLOSSARY.md. Assumptions: ASSUMPTIONS.md. Reused work: REUSE.md.

> **Person 3 layer (cloud decision plane) is in `cloud/`**: context-aware intent parsing, fleet health
> check, Rule Checker, DynamoDB lease manager, re-planning (lease v2), renewal, fallback, Bedrock and
> DynamoDB adapters. What it is, how to run it, and how to connect to AWS: `docs/p3/05-implementation.md`.

## Run it (one command)

```bash
python server.py          # http://127.0.0.1:8000  (PORT / DATA_DIR env vars)
```

Then open http://127.0.0.1:8000 and send **"Need an infusion pump at 2B-12,
urgent."**: the app asks the paediatric-pump question, your answer produces a
schema-valid lease v1, and the timeline replays the whole decision chain.
State is file-backed under `data/`, so a refresh or restart loses nothing.

Tests:

```bash
pip install -r requirements.txt   # jsonschema + pytest only
python -m pytest tests/ -q
```

Terraform (skeleton, never applied):

```bash
cd infra/terraform
terraform init -backend=false
terraform validate        # passes; do NOT apply without the account owner
```

## What is real, what is a stand-in, what Person 3 replaces

| Piece | Status |
|---|---|
| Contracts in `contracts/` (lease, escalation, robot sub-task, decision log) | **Real**: the team wire format; change via PR, everyone reviews |
| Decision log (FR-20) + `GET /tasks/{id}/timeline` | **Real**: append-only JSONL, replayable |
| Requester app (FR-21) | **Real**: static page, documented API only |
| FR-8 shape at the ingress (repeated `message_id` ignored, outdated `lease_version` dropped) | **Real** as a shape; edge-side enforcement is Person 2 |
| Cloud decisions (parse, clarify, propose, lease) | **Stand-in** (`standin.py` rules): Person 3 replaces with Bedrock + scheduler + rule checker |
| Storage (`data/*.json`, JSONL) | **Stand-in**: Person 3 swaps `store.py` calls for DynamoDB (tables already in Terraform) |
| Result delivery (`OUTPUT_READY` completing a task) | **Stand-in** ingress; Person 3 wires real edge escalations via SQS/IoT |
| Token/cost numbers | **Stand-in values** (`cost.py` counters; `len//4` tokens, 150-token cap): Person 3 feeds real Bedrock usage |
| AWS deployment | **Skeleton only** (`infra/terraform/`), validates but never applied |

## API

| Method + path | Purpose |
|---|---|
| `POST /requests` `{text, message_id}` | Plain-language request → task; status + clarifying question when the stand-in is unsure. Repeated `message_id` → 200 `{duplicate: true}`, no action (FR-8). |
| `POST /requests/{id}/answer` `{text, message_id}` | Answer a clarifying question → lease v1 issued. |
| `POST /escalations` | Edge escalation (CLARIFY / UNEXPECTED_EVENT / OUTPUT_READY per PRD Table 2). Schema-validated, deduped, stale versions dropped. OUTPUT_READY completes the task. |
| `POST /checkins` | Edge check-in (`contracts/topics.md`): one `edge` decision-log row, deduped by `message_id`; no re-planning. |
| `POST /tasks/{id}/control` | Operator pause/cancel: a recorded intent only (one `rule` log row, status change); nothing is delivered to robots. |
| `GET /tasks/{id}` | Status, question, lease, result. |
| `GET /tasks/{id}/timeline` | Full decision log for the task (FR-20 replay). |
| `GET /tasks` | All tasks (app status view). |
| `GET /operator` | Minimal operator console (FR-22 slice): task list + timeline + pause/cancel intents. |
| `GET /outbox` | Lease publications recorded in `data/outbox.jsonl` (the messaging stand-in Person 2 reads). |
| `GET /schemas/{name}.json` | The published contracts (Person 3's plug-in point). |
| `GET /cost` | PRD Sec. 7 counters + task count. |

## Cost counters (PRD Sec. 7)

`GET /cost` returns every term of
`Cost = [T_in·p_in + T_out·p_out + F·p_fargate + M·p_iot + W·p_ddb + Q·p_sqs
+ S·p_s3 + D·p_transfer + G·p_apigw + K·p_cw] / N_tasks` as raw counters
(`input_tokens`, `output_tokens`, `fargate_gbs`, `iot_messages`,
`dynamodb_ops`, `sqs_messages`, `s3_ops`, `data_transfer_bytes`,
`apigw_requests`, `cloudwatch_metrics`) plus `tasks` (= N_tasks). Prices stay
symbolic per the PRD; no Cost Explorer calls.

## Layout

```
contracts/          JSON Schema wire contracts (draft 2020-12)
contracts.py        schema loading + validation + cross-field rules
store.py            file-backed task store + append-only decision log
standin.py          the cloud stand-in (Person 3 replaces this file)
cost.py             PRD Sec. 7 counters
server.py           stdlib HTTP server: API + static app
static/             requester app (no build step, no framework)
infra/terraform/    AWS skeleton, terraform validate passes
tests/              public-behaviour tests (HTTP + schemas only)
```
