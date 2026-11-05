"""Check, against real AWS, every integration that the offline tests can only emulate. YOU run this (it needs your credentials).

    export AWS_PROFILE=nimbus-p3 AWS_REGION=ap-south-1
    export NIMBUS_DDB_PREFIX=nimbus-ds252 NIMBUS_RESULTS_BUCKET=<bucket> NIMBUS_SQS_QUEUE_URL=<url>   # whichever exist
    export NIMBUS_BEDROCK_MODEL=apac.amazon.nova-lite-v1:0  NIMBUS_IOT_ENDPOINT=<xxxx-ats.iot.ap-south-1.amazonaws.com>  NIMBUS_KB_ID=<id>
    python scripts/aws_verify.py                 # all checks
    python scripts/aws_verify.py --only s3,sqs   # some

Each check is independent and prints PASS, FAIL (with the error) or SKIP (the variable it needs is not set). Probe objects use a
`verify-` prefix and are removed. Nothing here creates infrastructure. Exit code 1 if any check FAILS. See docs/aws-verify.md.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid

REGION = os.environ.get("AWS_REGION", "ap-south-1")


def need(*names):
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise Skip("set " + ", ".join(missing))


class Skip(Exception):
    pass


def check_identity(boto3):
    ident = boto3.client("sts", region_name=REGION).get_caller_identity()
    return f"account {ident['Account']}, {ident['Arn'].split('/')[-1]}"


def check_bedrock(boto3):
    need("NIMBUS_BEDROCK_MODEL")
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
    from cloud.llm import BedrockLLM
    from cloud.pack import Pack
    from cloud.tools import ToolRegistry
    pack = Pack.load()
    llm = BedrockLLM(os.environ["NIMBUS_BEDROCK_MODEL"], ToolRegistry(pack), REGION)
    t0 = time.time()
    res = llm.converse(system=pack.prompts["intent"], messages=[{"role": "user", "text": "Deliver a wheelchair to ward 3, routine."}],
                       tools=ToolRegistry(pack).names("intent"))
    ok = bool(res.tool_calls)
    if not ok:
        raise RuntimeError("the model answered without calling a tool")
    return f"{res.tool_calls[0]['name']} in {time.time() - t0:.1f}s ({res.input_tokens} in / {res.output_tokens} out tokens)"


def check_dynamodb(boto3):
    prefix = os.environ.get("NIMBUS_DDB_PREFIX", "nimbus-ds252")
    c = boto3.client("dynamodb", region_name=REGION)
    names = [f"{prefix}-{t}" for t in ("state", "leases", "tasks", "decision_log", "robot_status")]
    missing = [n for n in names if n not in c.list_tables()["TableNames"]]
    if missing:
        raise RuntimeError(f"tables not found: {missing} (is NIMBUS_DDB_PREFIX right? did P4 apply Terraform?)")
    key = f"DUR#verify#{uuid.uuid4().hex[:6]}"
    c.put_item(TableName=names[0], Item={"pk": {"S": key}, "value": {"S": "{}"}})
    got = c.get_item(TableName=names[0], Key={"pk": {"S": key}}, ConsistentRead=True)["Item"]
    c.delete_item(TableName=names[0], Key={"pk": {"S": key}})
    return f"5 tables present; write/read/delete on {names[0]} ok" if got else "read failed"


def check_s3(boto3):
    need("NIMBUS_RESULTS_BUCKET")
    from_root = os.path.dirname(os.path.abspath(__file__)) + "/.."
    sys.path.insert(0, from_root)
    from cloud.results import ResultStore
    bucket, s3 = os.environ["NIMBUS_RESULTS_BUCKET"], boto3.client("s3", region_name=REGION)
    store = ResultStore(bucket=bucket, client=s3)
    key = f"results/verify-{uuid.uuid4().hex[:6]}.json"
    ref = store.put_json(key, {"verify": True})
    if not ref:
        raise RuntimeError("put failed (IAM: s3:PutObject on results/*?)")
    info = store.describe(ref)
    s3.delete_object(Bucket=bucket, Key=key)
    if not info.get("readable") or '"verify"' not in info.get("preview", ""):
        raise RuntimeError(f"could not read it back: {info}")
    return f"put, head and preview of {ref} ok, then deleted; a foreign bucket is refused: {not store.describe('s3://some-other-bucket/x').get('readable')}"


def check_sqs(boto3):
    need("NIMBUS_SQS_QUEUE_URL")
    q, sqs = os.environ["NIMBUS_SQS_QUEUE_URL"], boto3.client("sqs", region_name=REGION)
    tag = uuid.uuid4().hex[:8]
    sqs.send_message(QueueUrl=q, MessageBody=json.dumps({"verify": tag}))
    for _ in range(5):
        got = sqs.receive_message(QueueUrl=q, MaxNumberOfMessages=10, WaitTimeSeconds=3).get("Messages", [])
        mine = [m for m in got if tag in m["Body"]]
        if mine:
            sqs.delete_message(QueueUrl=q, ReceiptHandle=mine[0]["ReceiptHandle"])
            return "send, receive and delete ok (IAM allows the consumer's actions)"
    raise RuntimeError("sent a message but did not receive it back")


def check_iot(boto3):
    need("NIMBUS_IOT_ENDPOINT")
    iot = boto3.client("iot-data", region_name=REGION, endpoint_url=f"https://{os.environ['NIMBUS_IOT_ENDPOINT']}")
    iot.publish(topic="nimbus/verify/probe", qos=1, payload=json.dumps({"type": "PROBE", "message_id": f"verify-{uuid.uuid4().hex[:6]}"}).encode())
    return "published to nimbus/verify/probe at QoS 1 (see it in the IoT console MQTT test client; the IoT rule should land it on the queue)"


def check_kb(boto3):
    need("NIMBUS_KB_ID")
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
    from cloud.retrieval import BedrockKBRetriever
    docs = BedrockKBRetriever(os.environ["NIMBUS_KB_ID"], region=REGION).retrieve("infusion pump to a paediatric ward", limit=2)
    if not docs:
        raise RuntimeError("the Knowledge Base returned nothing (has the data source been synced? run scripts/kb_sync.py, then Sync in the console)")
    return f"{len(docs)} passage(s): " + ", ".join(d["id"] for d in docs)


CHECKS = {"identity": check_identity, "bedrock": check_bedrock, "dynamodb": check_dynamodb, "s3": check_s3, "sqs": check_sqs, "iot": check_iot, "kb": check_kb}


def run(only: list[str] | None = None, boto3_module=None, out=print) -> dict:
    if boto3_module is None:
        import boto3 as boto3_module
    results = {}
    for name, fn in CHECKS.items():
        if only and name not in only:
            continue
        try:
            results[name] = ("PASS", fn(boto3_module))
        except Skip as s:
            results[name] = ("SKIP", str(s))
        except Exception as exc:
            results[name] = ("FAIL", f"{type(exc).__name__}: {exc}")
        out(f"{results[name][0]:<5} {name:<9} {results[name][1]}")
    return results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default="")
    a = ap.parse_args(argv)
    results = run([x for x in a.only.split(",") if x] or None)
    print("\nRESULT:", "FAIL" if any(s == "FAIL" for s, _ in results.values()) else "PASS (SKIPs are checks whose variables were not set)")
    return 1 if any(s == "FAIL" for s, _ in results.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
