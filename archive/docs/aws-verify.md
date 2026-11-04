# Verifying the AWS side (you run this; I cannot)

Everything below is built and tested offline (emulated DynamoDB, S3, SQS; stubbed Bedrock, IoT, Knowledge Base). Nothing from MS2 or later has
run on real AWS yet. This is the checklist for doing it, in order, with who to ask when a step fails.

## 0. Prerequisites (P4 applies Terraform; you must not apply it unasked)

- Terraform from this branch applied by P4: tables (incl. `robot_status`), the outputs bucket, the escalations queue, the task role with
  `infra/terraform/p3.tf` (adds S3 `results/*` `logs/*` `bundles/*`, SQS receive/delete, `bedrock:Retrieve`, IoT `notice`), and the Fargate environment
  (`NIMBUS_RESULTS_BUCKET`, `NIMBUS_SQS_QUEUE_URL`, `NIMBUS_EMF`, `NIMBUS_KB_ID`).
- The IoT rule that lands edge messages on the queue (P4). Suggested SQL, so the consumer gets the topic with the payload:
  `SELECT *, topic() AS topic FROM 'nimbus/+/checkin'` and the same for `'nimbus/+/escalation'`, action: SQS `escalations`. Give the queue a
  dead-letter queue (redrive after 5 receives).
- Bedrock model access in the account (Nova Lite via the `apac.` inference profile, as in MS1).

## 1. Run the checks

```bash
export AWS_PROFILE=nimbus-p3 AWS_REGION=ap-south-1
export NIMBUS_DDB_PREFIX=nimbus-ds252
export NIMBUS_RESULTS_BUCKET=<outputs bucket>   NIMBUS_SQS_QUEUE_URL=<queue url>
export NIMBUS_BEDROCK_MODEL=apac.amazon.nova-lite-v1:0   NIMBUS_IOT_ENDPOINT=<xxxx-ats.iot.ap-south-1.amazonaws.com>
python scripts/aws_verify.py
```

| Check | PASS means | If it FAILS |
|---|---|---|
| identity | credentials work, shows the account | `aws sso login` / profile |
| bedrock | a tool call came back, with its latency | model access or the inference-profile id (P3) |
| dynamodb | all 5 tables exist; write/read/delete on `state` | wrong prefix, or P4 has not applied Terraform |
| s3 | put, head, preview, delete under `results/`; a foreign bucket is refused | IAM `ResultsAndLogs` (P3's `p3.tf`) |
| sqs | send, receive, delete | IAM `EdgeQueue` (P3's `p3.tf`) |
| iot | a QoS 1 publish to `nimbus/verify/probe` is accepted | endpoint value, or `iot:Publish` for that topic |
| kb | passages come back for a paediatric-pump query | KB id, data source not synced (see section 3) |

## 2. Run the service on AWS and watch it

```bash
python scripts/smoke_live.py --model $NIMBUS_BEDROCK_MODEL --teardown       # MS1 smoke: Bedrock + DynamoDB end to end
NIMBUS_BACKEND=aws NIMBUS_LLM=bedrock python server.py                      # then open /map, run SC-1..SC-3
python scripts/trace.py --last --detail                                     # each model call with its latency
curl localhost:8000/metrics                                                 # request_to_lease_s p95 is the NFR-1 number (<= 30 s)
```

With `NIMBUS_EMF=1` the service prints CloudWatch Embedded Metric Format lines every 60 s; in CloudWatch the namespace `Nimbus` then shows
`QueueDepth`, `LiveLeases`, `RequestToLeaseP95`, `LlmLatencyP95`, `HealthyClusters`.

## 3. Optional: the rule Knowledge Base (FR-19, decision DEC-11)

Default is keyword retrieval from the pack (no AWS). To use a Knowledge Base instead:
`python scripts/kb_sync.py --pack hospital_sim --bucket <bucket> --prefix nimbus/kb`, create a Knowledge Base whose data source is that prefix, sync it,
set `NIMBUS_KB_ID` (Terraform variable `knowledge_base_id`). Rules are still enforced from the pack by the Rule Checker; the Knowledge Base only
chooses which rule texts the model reads, and an active restriction's rule is always included.

## 4. The two numbers only a real run gives

```bash
NIMBUS_LLM=bedrock NIMBUS_BEDROCK_MODEL=$NIMBUS_BEDROCK_MODEL NIMBUS_CASSETTE=data/e3.cassette.json NIMBUS_CASSETTE_MODE=record python scripts/run_e3.py --seeds 1
NIMBUS_LLM=bedrock NIMBUS_CASSETTE=data/e3.cassette.json NIMBUS_CASSETTE_MODE=replay python scripts/run_e3.py --seeds 10 && python scripts/e3_report.py
NIMBUS_LLM=bedrock NIMBUS_CASSETTE=data/e2.cassette.json NIMBUS_CASSETTE_MODE=record python scripts/score_e2.py --config C1      # after you review evaluation/e2 labels
```
The first records each distinct prompt once (about 100 Nova Lite calls); the second replays them for free with 10 seeds, byte-identically; `/metrics`
gives the measured request-to-lease p95 (NFR-1) from a live run.
