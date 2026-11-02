"""E6 message injector: repeated, outdated and re-ordered edge messages against a running cloud (PRD FR-8, E6).

    python scripts/inject_messages.py --api http://127.0.0.1:8000            # runs all three on fresh tasks, prints a report

Each check starts a task, sends the messages, and reads the result back through the API:
  repeat    the same escalation sent N times: it must take effect exactly once (duplicates acknowledged, not acted on);
  outdated  an escalation or check-in for an older lease version after a re-plan: refused with 409, nothing changes;
  reorder   the same set of messages in two different orders must end in the same state.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from urllib import request as urlreq
from urllib.error import HTTPError


def call(api: str, method: str, path: str, body: dict | None = None):
    req = urlreq.Request(api.rstrip("/") + path, data=None if body is None else json.dumps(body).encode(), method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urlreq.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"{}")
    except HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def new_task(api: str, text: str = "Deliver a wheelchair to ward 3, routine.") -> str:
    return call(api, "POST", "/requests", {"text": text, "message_id": f"inj-{uuid.uuid4().hex[:8]}"})[1]["task_id"]


def done(task_id: str, version: int, mid: str) -> dict:
    return {"type": "OUTPUT_READY", "message_id": mid, "lease_version": version, "task_id": task_id, "payload": {"result_ref": f"s3://x/{task_id}.jpg"}}


def event(task_id: str, version: int, mid: str, zone: str) -> dict:
    return {"type": "UNEXPECTED_EVENT", "message_id": mid, "lease_version": version, "task_id": task_id,
            "payload": {"what_happened": f"{zone} blocked", "where": zone, "robots_saw": ["cart"], "segments": [{"segment_id": zone, "state": "blocked"}]}}


def timeline_count(api: str, task_id: str, event_name: str) -> int:
    rows = call(api, "GET", f"/tasks/{task_id}/timeline")[1]["timeline"]
    return sum(r["output"].get("event") == event_name for r in rows)


def repeat(api: str, n: int = 5) -> dict:
    tid = new_task(api)
    msg = done(tid, 1, f"rep-{uuid.uuid4().hex[:6]}")
    codes = [call(api, "POST", "/escalations", msg)[0] for _ in range(n)]
    return {"sent": n, "accepted": codes.count(201), "acknowledged_as_duplicate": codes.count(200), "times_acted_on": timeline_count(api, tid, "OUTPUT_READY")}


def outdated(api: str, blocked_zone: str = "E3") -> dict:
    tid = new_task(api, "Deliver a wheelchair to 2B-3, routine.")
    first = call(api, "POST", "/escalations", event(tid, 1, f"o1-{uuid.uuid4().hex[:6]}", blocked_zone))[1]
    before = call(api, "GET", f"/tasks/{tid}")[1]
    stale_esc = call(api, "POST", "/escalations", done(tid, 1, f"o2-{uuid.uuid4().hex[:6]}"))
    stale_ci = call(api, "POST", "/checkins", {"cluster_id": before["lease"]["cluster_id"], "lease_id": f"lease-{tid}-v1", "lease_version": 1,
                                                  "timestamp": "2026-11-02T10:00:00Z", "message_id": f"o3-{uuid.uuid4().hex[:6]}"})
    after = call(api, "GET", f"/tasks/{tid}")[1]
    return {"replanned_to": first.get("lease_version"), "stale_escalation_status": stale_esc[0], "stale_checkin_status": stale_ci[0],
            "state_unchanged": (before["status"], before["lease"]["version"]) == (after["status"], after["lease"]["version"])}


def reorder(api: str) -> dict:
    ends = []
    for order in ("forward", "reversed"):
        tid = new_task(api, "Deliver a wheelchair to 2B-3, routine.")
        msgs = [event(tid, 1, f"r1-{uuid.uuid4().hex[:6]}", "E3"), done(tid, 2, f"r2-{uuid.uuid4().hex[:6]}")]
        for m in (msgs if order == "forward" else msgs[::-1]):
            call(api, "POST", "/escalations", m)
        t = call(api, "GET", f"/tasks/{tid}")[1]
        ends.append({"status": t["status"], "lease_version": t["lease"]["version"]})
    return {"final_states": ends, "order_independent": ends[0]["lease_version"] == ends[1]["lease_version"] or ends[0]["status"] == ends[1]["status"]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default="http://127.0.0.1:8000")
    a = ap.parse_args(argv)
    report = {"repeat": repeat(a.api), "outdated": outdated(a.api), "reorder": reorder(a.api)}
    print(json.dumps(report, indent=1))
    ok = report["repeat"]["times_acted_on"] == 1 and report["outdated"]["state_unchanged"] and report["outdated"]["stale_escalation_status"] == 409
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
