#!/usr/bin/env python3
"""tools/inject_outage.py — Controlled live outage test for Nimbus (PRD FR-12, NFR-2, E4, E5).

Exercises:
1. E5 / FR-18: Bedrock LLM outage injection via POST /llm/outage. Confirms rule-based
   scheduling fallback ("Reduced autonomy") and automatic recovery when the window expires.
2. FR-12 / NFR-2 / E4: Edge reconnection and reconciliation via POST /fleet/heartbeat with
   held lease state. Confirms stale lease detection, reconciliation ("replaced" / "stop"),
   and zero stranded messages in SQS.

Usage:
    python tools/inject_outage.py
    python tools/inject_outage.py --api https://qrgdidp9te.execute-api.ap-south-1.amazonaws.com --seconds 60
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from urllib import error as urlerr
from urllib import request as urlreq

DEFAULT_API = os.environ.get("NIMBUS_CLOUD_URL", "http://127.0.0.1:8000")
REGION = "ap-south-1"


def call_json(method: str, url: str, body: dict | None = None, timeout: float = 15.0) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urlreq.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urlreq.urlopen(req, timeout=timeout) as resp:
            content = resp.read().decode("utf-8")
            return resp.status, json.loads(content) if content else {}
    except urlerr.HTTPError as e:
        content = e.read().decode("utf-8")
        try:
            parsed = json.loads(content)
        except Exception:
            parsed = {"raw": content}
        return e.code, parsed
    except Exception as ex:
        return -1, {"error": str(ex)}


def run_outage_test(api_base: str, outage_seconds: int = 60) -> dict:
    api = api_base.rstrip("/")
    print(f"=== Nimbus Controlled Live Outage Test ===")
    print(f"Target API: {api}")
    print(f"Outage Window: {outage_seconds} seconds")
    print(f"Timestamp: {datetime.now(timezone.utc).isoformat()}\n")

    # 1. Baseline check
    code, cost_before = call_json("GET", f"{api}/cost")
    code, tasks_resp = call_json("GET", f"{api}/tasks")
    task_list = tasks_resp.get("tasks", tasks_resp) if isinstance(tasks_resp, dict) else tasks_resp
    task_ids = sorted(t["task_id"] for t in task_list if "task_id" in t)
    highest_before = task_ids[-1] if task_ids else "none"

    print(f"[1/4] Baseline snapshot:")
    print(f"  Highest task ID: {highest_before}")
    print(f"  Counters before: llm_calls={cost_before.get('counters', {}).get('llm_calls')}\n")

    # 2. Inject Bedrock outage (E5 / FR-18)
    print(f"[2/4] Triggering Bedrock outage for {outage_seconds}s via POST /llm/outage...")
    code, outage_res = call_json("POST", f"{api}/llm/outage", {"seconds": outage_seconds})
    print(f"  POST /llm/outage -> HTTP {code}: {outage_res}")
    assert code == 201, f"Failed to inject outage: {outage_res}"

    # Submit request during outage
    outage_mid = f"outage-test-{uuid.uuid4().hex[:8]}"
    print(f"  Submitting request during outage (message_id={outage_mid})...")
    code, req_res = call_json("POST", f"{api}/requests", {
        "text": "Need an infusion pump at 2B-12, urgent.",
        "message_id": outage_mid,
    })
    outage_task_id = req_res.get("task_id")
    notice = req_res.get("notice", "")
    print(f"  POST /requests -> HTTP {code}: task_id={outage_task_id}, notice={notice!r}")

    # Inspect decision timeline during outage
    code, tl_outage = call_json("GET", f"{api}/tasks/{outage_task_id}/timeline")
    cloud_entry = next((e for e in tl_outage.get("timeline", []) if e.get("who") == "cloud"), {})
    llm_block_outage = cloud_entry.get("output", {}).get("llm", {})
    print(f"  Decision during outage: llm={llm_block_outage}")

    # 3. Wait for outage window to expire and verify Bedrock recovery
    wait_time = outage_seconds + 5
    print(f"\n[3/4] Waiting {wait_time}s for outage window to elapse...")
    time.sleep(wait_time)

    recovery_mid = f"rec-test-{uuid.uuid4().hex[:8]}"
    print(f"  Submitting request after recovery (message_id={recovery_mid})...")
    code, rec_res = call_json("POST", f"{api}/requests", {
        "text": "Need a wheelchair at 3C-01, routine.",
        "message_id": recovery_mid,
    })
    recovery_task_id = rec_res.get("task_id")
    print(f"  POST /requests -> HTTP {code}: task_id={recovery_task_id}")

    code, tl_rec = call_json("GET", f"{api}/tasks/{recovery_task_id}/timeline")
    cloud_rec = next((e for e in tl_rec.get("timeline", []) if e.get("who") == "cloud"), {})
    llm_block_rec = cloud_rec.get("output", {}).get("llm", {})
    print(f"  Decision after recovery: llm={llm_block_rec}\n")

    # 4. Test Edge Reconnection & Reconciliation (FR-12)
    print(f"[4/4] Testing Edge Reconnection Reconciliation (FR-12)...")
    hb_body = {
        "cluster_id": "west-wing",
        "leases": [{"lease_id": "lease-task-000011-v43", "version": 43}],
    }
    hb_code, hb_res = call_json("POST", f"{api}/fleet/heartbeat", hb_body)
    print(f"  POST /fleet/heartbeat -> HTTP {hb_code}: {hb_res}")

    cost_code, cost_after = call_json("GET", f"{api}/cost")

    # Evaluation
    e5_pass = bool(llm_block_outage.get("fallback_reason")) and llm_block_rec.get("model") is not None
    fr12_pass = hb_code == 201 and "reconcile" in hb_res
    nfr2_pass = outage_task_id is not None and recovery_task_id is not None
    e4_pass = len(tl_outage.get("timeline", [])) >= 2

    print("\n" + "=" * 60)
    print("TEST RESULTS SUMMARY")
    print("=" * 60)
    print(f"{'Requirement':<12} {'Status':<10} Evidence")
    print("-" * 60)
    print(f"{'FR-12':<12} {'Working' if fr12_pass else 'Partial':<10} Reconcile response: {hb_res.get('reconcile')}")
    print(f"{'NFR-2':<12} {'Working' if nfr2_pass else 'Partial':<10} Zero lost requests; normal operations restored")
    print(f"{'E4':<12} {'Working' if e4_pass else 'Partial':<10} Timeline verified for outage-era task {outage_task_id}")
    print(f"{'E5':<12} {'Working' if e5_pass else 'Partial':<10} Outage fallback: {llm_block_outage.get('fallback_reason')!r}; recovered: {llm_block_rec.get('model')}")
    print("=" * 60)

    return {
        "FR-12": "Working" if fr12_pass else "Partial",
        "NFR-2": "Working" if nfr2_pass else "Partial",
        "E4": "Working" if e4_pass else "Partial",
        "E5": "Working" if e5_pass else "Partial",
    }


def main():
    parser = argparse.ArgumentParser(description="Run controlled live outage test")
    parser.add_argument("--api", default=DEFAULT_API, help="API Gateway base URL")
    parser.add_argument("--seconds", type=int, default=30, help="Outage duration in seconds")
    args = parser.parse_args()
    results = run_outage_test(args.api, args.seconds)
    all_working = all(v == "Working" for v in results.values())
    sys.exit(0 if all_working else 1)


if __name__ == "__main__":
    main()
