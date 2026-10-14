# Cloud decision plane (Person 3): what it is and how to run it

The code in `cloud/` replaces `standin.py`. The HTTP routes and wire contracts are unchanged.

## What happens to a request

1. **Health check:** which clusters and robots are alive (check-ins and heartbeats). A quiet cluster gets a `probe`.
2. **Context:** items, places, free robots, stock, active restrictions and the task's own history.
3. **Intent:** the LLM turns the text into a task spec, or asks one clarifying question.
4. **Allocation:** the LLM proposes robots and a route. The **Rule Checker** accepts or rejects the proposal.
5. **Lease:** written with a conditional write, so a robot never holds two live leases. Extra requests wait in a queue.
6. **Later:** a check-in renews the lease (same version, new `expiry_ts`). An UNEXPECTED_EVENT triggers a re-plan (lease v2). OUTPUT_READY finishes the task.
7. **Fallback:** if the LLM fails or times out, a rules-only path takes over and the requester sees a reduced-autonomy notice.

Every decision is a row in the decision log. Rows carry no timings, so a replay of the same run is identical.

## Domain packs

All domain knowledge lives in `packs/<name>/` (map, fleet, rules, prompts, scenarios). `packs/DEFAULT` names the pack in use (`hospital`). A test fails if a domain word appears in `cloud/*.py`. A second pack, `rescue`, proves the code is not hospital-specific.

## Run it

```bash
pip install -r requirements.txt            # add requirements-aws.txt for the AWS backend
python server.py                           # http://127.0.0.1:8000
python scripts/edge_sim.py --once --complete   # a fake edge: reads leases, checks in, reports done
python -m pytest -q                        # all tests, no AWS needed
```

## Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `NIMBUS_LLM` | `fake` | `bedrock` to call Amazon Bedrock |
| `NIMBUS_BEDROCK_MODEL` | none | Model or inference-profile id, for example `apac.amazon.nova-lite-v1:0` |
| `AWS_REGION` | `ap-south-1` | Region for Bedrock and DynamoDB |
| `NIMBUS_BACKEND` | `local` | `aws` stores tasks and leases in DynamoDB instead of `data/` |
| `NIMBUS_DDB_PREFIX` | `nimbus-ds252` | Table name prefix |
| `NIMBUS_IOT_ENDPOINT` | none | When set, leases are also published to AWS IoT Core |
| `NIMBUS_MODE` | `C1` | Evaluation configuration (C1 proposed, C2 cloud-only, C3 no-LLM) |
| `NIMBUS_PACK` | `packs/DEFAULT` | Which domain pack to load |
| `NIMBUS_CASSETTE`, `NIMBUS_CASSETTE_MODE` | none, `replay` | Record real LLM calls once, then replay them for free |

## AWS

* **Running service:** gets Bedrock and DynamoDB access from its IAM **task role** (`infra/terraform/p3.tf`), so no keys are stored anywhere.
* **A developer's laptop:** uses that person's own IAM user with `infra/iam/p3-dev-policy.json`.
* **Quotas:** Bedrock quotas belong to the AWS account and region, not to a user.
* **Live checks** (run by a person with credentials): `scripts/bedrock_hello.py`, then `scripts/smoke_live.py --teardown`. The smoke test creates throwaway tables and deletes them.

## Tests

* `tests/test_api.py` and `tests/test_cloud_api.py` go through HTTP.
* `tests/test_cloud_*.py` and `tests/test_packs.py` test `cloud/` directly; P4 agreed on 14 Oct.
* `tests/test_cloud_aws.py` runs the DynamoDB backend against an emulator (moto).
