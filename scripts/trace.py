"""Print what happened for a task, in order, as a ladder: who said what to whom, which model calls were made
(with what they were asked and what they answered), and what the cloud decided. Works against a running server.

    python scripts/trace.py task-000003                 # one task
    python scripts/trace.py --last                      # the most recent task
    python scripts/trace.py task-000003 --detail        # also show each model prompt/answer and message payloads
    python scripts/trace.py --follow task-000003        # keep printing as new events arrive
    python scripts/trace.py --all                       # everything the server saw, heartbeats included

Time is wall-clock (HH:MM:SS.mmm). The model calls show their latency, so you can see where the seconds go.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from urllib import request as urlreq

ARROW = {"requester": "REQUESTER", "cloud": "CLOUD", "rule": "RULE", "model": "MODEL", "edge": "EDGE", "log": "log"}
KIND_MARK = {"llm": "◆", "llm-error": "✖", "decision": "·", "lease": "▶", "renewal": "↻", "probe": "?", "notice": "✉"}


def get(api: str, path: str) -> dict:
    with urlreq.urlopen(api.rstrip("/") + path) as r:
        return json.loads(r.read().decode())


def line(e: dict, detail: bool) -> str:
    t = e["ts"][11:23]
    src, dst = ARROW.get(e["from"], e["from"]), ARROW.get(e["to"], e["to"])
    who = f"{src}→{dst}" if src != dst and dst != "log" else src
    out = f"{t}  {KIND_MARK.get(e['kind'], ' ')} {who:<22} {e['summary']}"
    if detail:
        d = e["detail"]
        if e["kind"] == "llm":
            out += f"\n{'':37}asked:    {d['asked'][:300]!r}\n{'':37}answered: {json.dumps(d['answered'])[:400]}"
        elif e["kind"] in ("lease", "notice"):
            out += f"\n{'':37}{json.dumps(d['payload'])[:500]}"
        elif e["kind"] == "decision":
            out += f"\n{'':37}in:  {json.dumps(d['inputs'])[:300]}\n{'':37}out: {json.dumps(d['output'])[:300]}"
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("task_id", nargs="?")
    ap.add_argument("--api", default="http://127.0.0.1:8000")
    ap.add_argument("--last", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--detail", action="store_true")
    ap.add_argument("--follow", action="store_true")
    a = ap.parse_args()
    tid = a.task_id
    if a.last:
        tasks = get(a.api, "/tasks")["tasks"]
        if not tasks:
            sys.exit("no tasks yet")
        tid = tasks[-1]["task_id"]
    if not tid and not a.all:
        ap.error("give a task id, --last or --all")
    after = 0
    while True:
        evs = get(a.api, f"/trace?after={after}&limit=400")["events"] if a.all else \
            [e for e in get(a.api, f"/tasks/{tid}/trace")["events"] if e["seq"] > after]
        for e in evs:
            print(line(e, a.detail), flush=True)
            after = max(after, e["seq"])
        if not a.follow:
            break
        time.sleep(1.0)


if __name__ == "__main__":
    main()
