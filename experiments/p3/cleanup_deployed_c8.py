"""Cancel the C8 load-test tasks that were left open on the deployed cloud when the run was interrupted.

Only tasks that (a) were created at or after the first task id recorded in results/final/C8/raw/requests.jsonl and
(b) carry one of C8's fixed request texts (c8_load.TEXTS) are touched; it prints them first and asks before cancelling.
Cancelling frees the robot and returns the reserved item (stock).

    python experiments/p3/cleanup_deployed_c8.py
"""

from __future__ import annotations

import collections
import json
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import c8_load  # noqa: E402
import lib  # noqa: E402


def main() -> int:
    api = lib.Api(lib.DEFAULT_DEPLOYED)
    raw = [json.loads(line)["task_id"] for line in (lib.RESULTS / "C8" / "raw" / "requests.jsonl").open() if json.loads(line).get("task_id")]
    first = min(int(t.split("-")[1]) for t in raw)
    texts = set(c8_load.TEXTS)
    st, t, _ = api.get("/tasks")
    mine = [x for x in t["tasks"] if int(x["task_id"].split("-")[1]) >= first and x["request_text"] in texts
            and x["status"] not in ("completed", "cancelled", "failed", "rejected")]
    print(f"C8 run started at task-{first:06d}; open C8 tasks: {len(mine)} {dict(collections.Counter(x['status'] for x in mine))}")
    for x in mine[:10]:
        print("  ", x["task_id"], x["status"], x["request_text"])
    if not mine or input("cancel these? [y/N] ").strip().lower() != "y":
        return 0

    def cancel(x):
        return api.post(f"/tasks/{x['task_id']}/control", {"action": "cancel", "message_id": f"exp-cancel-{uuid.uuid4().hex[:10]}"})[0]

    with ThreadPoolExecutor(8) as ex:
        print(dict(collections.Counter(ex.map(cancel, mine))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
