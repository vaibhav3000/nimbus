"""Step 2 of connecting to AWS: run SC-1 end to end on REAL Bedrock + REAL
DynamoDB (and optionally IoT), from this machine, then clean up.

    python scripts/smoke_live.py --model <id> --cassette data/sc1.cassette.json
    python scripts/smoke_live.py --model <id> --teardown      # delete the temp tables afterwards

Creates tables named nimbus-smoke-<random>-* (pay-per-request: cents at most),
never touches the project's real tables, and with --teardown removes them.
With --cassette the model calls are recorded so later runs can replay them for
free (set NIMBUS_CASSETTE=<file> NIMBUS_CASSETTE_MODE=replay).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib import request as urlreq

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))


def call(base, method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urlreq.Request(base + path, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        r = urlreq.urlopen(req)
    except Exception as e:  # HTTPError carries the body
        r = e
    with r:
        return r.status if hasattr(r, "status") else r.code, json.loads(r.read().decode())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, help="Bedrock model id or inference profile id")
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "ap-south-1"))
    ap.add_argument("--cassette", help="record model calls to this file")
    ap.add_argument("--teardown", action="store_true", help="delete the temporary tables at the end")
    a = ap.parse_args()

    prefix = f"nimbus-smoke-{uuid.uuid4().hex[:6]}"
    os.environ.update({"NIMBUS_BACKEND": "aws", "NIMBUS_LLM": "bedrock", "NIMBUS_BEDROCK_MODEL": a.model,
                       "AWS_REGION": a.region, "NIMBUS_DDB_PREFIX": prefix, "NIMBUS_MODE": "C1"})
    if a.cassette:
        os.environ.update({"NIMBUS_CASSETTE": a.cassette, "NIMBUS_CASSETTE_MODE": "record"})

    import boto3

    from cloud.aws_dynamo import ensure_tables, tables_from_env
    import server
    from edge_sim import EdgeSim

    ddb = boto3.client("dynamodb", region_name=a.region)
    names = tables_from_env()
    print(f"creating tables {prefix}-* in {a.region} ...")
    ensure_tables(ddb, names)
    for n in names.values():
        ddb.get_waiter("table_exists").wait(TableName=n)

    httpd = server.make_server("127.0.0.1", 0, Path(tempfile.mkdtemp()))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    failures = []

    def step(label, fn):
        t0 = time.perf_counter()
        out = fn()
        print(f"  {label:42} {int((time.perf_counter() - t0) * 1000):6d} ms")
        return out

    try:
        print("SC-1 on real Bedrock + DynamoDB:")
        _, r = step("request -> model asks the paediatric question",
                    lambda: call(base, "POST", "/requests", {"text": "Need an infusion pump at 2B-12, urgent.", "message_id": "live-1"}))
        if r["status"] != "needs_clarification":
            failures.append(f"expected a clarifying question, got {r['status']}")
        tid = r["task_id"]
        print(f"    question: {r.get('question', {}).get('question')}")
        _, r = step("answer -> allocation -> check -> lease v1",
                    lambda: call(base, "POST", f"/requests/{tid}/answer", {"text": "Yes, a paediatric pump please.", "message_id": "live-2"}))
        if r["status"] != "leased":
            failures.append(f"expected leased, got {r['status']} ({r.get('reason')})")
        else:
            print(f"    lease {r['lease']['lease_id']} -> {r['lease']['cluster_id']} robots {r['lease']['assigned_robots']}")
        s, r = step("re-plan around E3 -> lease v2 (conditional write)",
                    lambda: call(base, "POST", f"/tasks/{tid}/replan", {"blocked_zones": ["E3"], "message_id": "live-3"}))
        if s != 201:
            failures.append(f"replan failed: {r}")
        sim = EdgeSim(base, "east-wing", complete=True)
        step("fake edge: check-in + OUTPUT_READY", sim.step)
        _, t = call(base, "GET", f"/tasks/{tid}")
        if t["status"] != "completed" or t["lease"]["version"] != 2:
            failures.append(f"final state wrong: {t['status']} v{t['lease']['version']}")
        _, tl = call(base, "GET", f"/tasks/{tid}/timeline")
        llm_rows = [r_["output"]["llm"] for r_ in tl["timeline"] if r_["output"].get("llm")]
        tin = sum(x.get("input_tokens", 0) for x in llm_rows)
        tout = sum(x.get("output_tokens", 0) for x in llm_rows)
        print(f"\nmodel {llm_rows[0].get('model') if llm_rows else '?'}: {tin} input / {tout} output tokens "
              f"(real usage; multiply by your model's per-token price)")
        print("timeline:", " > ".join(f"{r_['who']}:{r_['output'].get('decision') or r_['output'].get('event')}" for r_ in tl["timeline"]))
    finally:
        httpd.shutdown()
        if a.teardown:
            for n in names.values():
                ddb.delete_table(TableName=n)
            print(f"deleted tables {prefix}-*")
        else:
            print(f"tables {prefix}-* left in place; delete with --teardown or the console")
    print("\nRESULT:", "PASS" if not failures else "FAIL\n  " + "\n  ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
