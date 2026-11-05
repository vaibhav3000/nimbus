#!/usr/bin/env python3
"""End-to-end demo driver: staff requests -> cloud -> lease -> edge -> robots, until each task closes.

    python scripts/demo_e2e.py                       # SC-1 (clarify) then a West-wing delivery
    python scripts/demo_e2e.py --text "Need a wheelchair at ward 2C" --timeout 300
"""
import argparse, json, sys, time, urllib.request

def call(api, method, path, body=None):
    req = urllib.request.Request(api + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def run(api, text, tag, timeout):
    print(f"\n>>> REQUEST: {text!r}")
    t = call(api, "POST", "/requests", {"text": text, "message_id": f"{tag}-req"})
    tid = t["task_id"]
    if t["status"] == "needs_clarification":
        q = t["question"]
        print(f"    cloud asks: {q['question']}  -> answering with the first reading: {q['possible_readings'][0]}")
        t = call(api, "POST", f"/requests/{tid}/answer", {"text": q["possible_readings"][0], "message_id": f"{tag}-ans"})
    lease = t.get("lease")
    if lease:
        print(f"    lease {lease['lease_id']} -> {lease['cluster_id']} robots {lease['assigned_robots']} speed<={lease['speed_limit']} m/s")
    t0, last = time.time(), None
    while time.time() - t0 < timeout:
        t = call(api, "GET", f"/tasks/{tid}")
        if t["status"] != last:
            print(f"    [{time.time()-t0:5.0f}s] {tid}: {t['status']}"); last = t["status"]
        if t["status"] in ("completed", "cancelled", "rejected", "failed"):
            break
        time.sleep(2)
    print(f"<<< {tid} final: {t['status']}  result={t.get('result_ref')}")
    return t["status"] == "completed"

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://127.0.0.1:8000")
    ap.add_argument("--text", action="append")
    ap.add_argument("--timeout", type=int, default=240)
    a = ap.parse_args()
    texts = a.text or ["Need an infusion pump at 2B-12, urgent", "Please bring a wheelchair to ward 2C"]
    tag = str(int(time.time()))
    ok = [run(a.api, t, f"{tag}-{i}", a.timeout) for i, t in enumerate(texts)]
    print("\nRESULT:", "ALL TASKS COMPLETED" if all(ok) else "SOME TASKS DID NOT COMPLETE")
    sys.exit(0 if all(ok) else 1)
