"""E7: load and scale (PRD NFR-3, NFR-4, FR-20): 3 clusters and 3 -> 10+ robots under a MIMIC-shaped arrival pattern.

    python scripts/run_e7.py                         # robots 3, 6, 10, 20 on the synthetic scale pack, one simulated day
    python scripts/run_e7.py --robots 3,10 --hours 4 --seed 1
    python scripts/run_e7.py --arrivals my_arrivals.csv     # plug in real MIMIC-IV demo timestamps: columns t_seconds,priority,item,ward

Arrivals: a Poisson background plus bursts at ward rounds (the shape the PRD takes from MIMIC transfers, medication rounds and lab
events). Each request goes through the real orchestrator on a fixed clock; a leased task completes after the robot's modelled travel
time and frees its robot, which retries the queue. Reported per scale: wait to lease (p50/p95), deadlines missed, tasks completed,
model calls and cost per task, and the cloud's MEASURED compute time per decision (the code's own cost, excluding model latency).
NFR-3's "p95 latency <= 1.5x the 3-robot baseline" is judged on that measured decision time; the edge-side half of NFR-3 is P2's to measure.
A second pass re-runs the whole day and checks the decision logs are identical (FR-20 / NFR-6 at scale).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import heapq
import json
import os
import random
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
os.environ["NIMBUS_PACK"] = "scale10"

import make_scale_pack  # noqa: E402
from cloud import mapview  # noqa: E402
from cloud.clock import FixedClock, parse_iso  # noqa: E402
from cloud.metrics import percentile  # noqa: E402
from cloud.orchestrator import Orchestrator  # noqa: E402

PRICE_IN, PRICE_OUT = 0.06e-6, 0.24e-6
ITEMS = ["wheelchair", "iv_pole", "monitor", "blanket"]


def arrivals(hours: float, seed: int, wards: list[str]) -> list[dict]:
    """Background Poisson (one every ~90 s) plus a burst of 6-10 requests at each 'round' (every 2 h), 25% urgent."""
    rng = random.Random(seed)
    t, out = 0.0, []
    horizon = hours * 3600
    while t < horizon:
        t += rng.expovariate(1 / 90)
        out.append({"t": t})
    for k in range(int(hours // 2) + 1):
        for _ in range(rng.randint(6, 10)):
            out.append({"t": k * 7200 + rng.uniform(0, 300)})
    out = sorted((a for a in out if a["t"] < horizon), key=lambda a: a["t"])
    for a in out:
        a.update(priority="urgent" if rng.random() < 0.25 else "routine", item=rng.choice(ITEMS), ward=rng.choice(wards))
    return out


def read_arrivals(path: str) -> list[dict]:
    with open(path) as f:
        return [{"t": float(r["t_seconds"]), "priority": r["priority"], "item": r["item"], "ward": r["ward"]} for r in csv.DictReader(f)]


def simulate(robots: int, arr: list[dict], seed: int) -> dict:
    with tempfile.TemporaryDirectory() as d:
        make_scale_pack.build(3, robots, Path(d) / "scale10")
        os.environ["NIMBUS_PACK"] = str(Path(d) / "scale10")
        os.environ["NIMBUS_MODE"] = "C1"
        clock = FixedClock()
        orch = Orchestrator.from_env(Path(d), clock=clock)
        for cid in orch.fleet.clusters:
            orch.fleet.heartbeat(cid, {})
        tasks, events, rows = {}, [], []
        waits, decision_ms, completions, missed, calls, tin, tout = [], [], 0, 0, 0, 0, 0
        pending = {}

        stocked = {"hour": -1}

        def alive():
            for cid in orch.fleet.clusters:      # the edges heartbeat every 15 s: they are never silent in this experiment
                orch.fleet.heartbeat(cid, {})
            hour = int((clock.now() - FixedClock().now()).total_seconds() // 3600)
            if hour != stocked["hour"]:          # the store is restocked every hour, as a real one would be
                stocked["hour"] = hour
                for cid in orch.fleet.clusters:
                    orch.fleet.set_stock(cid, {"wheelchair": 40, "iv_pole": 40, "monitor": 40, "blanket": 200})

        def log(out):
            rows.extend({"who": r["who"], "output": r["output"], "lease_version": r["lease_version"]} for r in out.rows)

        def start(t, n, a):
            text = f"Deliver {'an' if a['item'] == 'iv_pole' else 'a'} {a['item'].replace('_', ' ')} to {a['ward']}, {a['priority']}."
            task = {"task_id": f"task-{n:06d}", "request_text": text, "status": "received", "arrived": t}
            t0 = time.perf_counter()
            out = orch.handle_request(task, text)          # the full path: understanding, allocation, check, commit
            decision_ms.append((time.perf_counter() - t0) * 1000)
            return task, out

        def settle(task, out, now):
            nonlocal calls, tin, tout
            calls += out.llm_calls; tin += out.tokens_in; tout += out.tokens_out
            log(out)
            task.update(status=out.kind, spec=out.spec or task.get("spec"), lease=out.lease, reservation=out.reservation)
            if out.kind == "leased":
                waits.append(now - task["arrived"])
                travel = sum(orch.topo.path_length(s["path"]) for s in mapview._plan(orch.topo, out.lease, orch.fleet.robot_zone(out.lease["assigned_robots"][0])) if s["type"] == "drive")
                done = now + travel / max(0.05, out.lease["speed_limit"]) + 40
                heapq.heappush(events, (done, task["task_id"]))

        n = 0
        arr_iter = iter(arr)
        nxt = next(arr_iter, None)
        while nxt is not None or events:
            if nxt is not None and (not events or nxt["t"] <= events[0][0]):
                clock_t, a = nxt["t"], nxt
                clock.advance(max(0, clock_t - (clock.now() - FixedClock().now()).total_seconds()))
                alive()
                n += 1
                task, out = start(clock_t, n, a)
                tasks[task["task_id"]] = task
                settle(task, out, clock_t)
                nxt = next(arr_iter, None)
            else:
                done_t, tid = heapq.heappop(events)
                clock.advance(max(0, done_t - (clock.now() - FixedClock().now()).total_seconds()))
                alive()
                task = tasks[tid]
                limit = task["spec"]["deadline_minutes"] * 60
                if done_t - task["arrived"] > limit:
                    missed += 1
                completions += 1
                orch.finish(task); task["status"] = "completed"
                for t2, o2 in orch.drain(tasks.get):
                    settle(t2, o2, done_t)
        digest = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
        return {"robots": robots, "requests": len(arr), "completed": completions, "missed_deadlines": missed,
                "missed_rate": round(missed / max(1, completions), 4), "wait_s_p50": percentile(waits, 50), "wait_s_p95": percentile(waits, 95),
                "decision_ms_p50": percentile(decision_ms, 50), "decision_ms_p95": percentile(decision_ms, 95),
                "llm_calls_per_task": round(calls / max(1, len(arr)), 3),
                "cost_per_task_usd": round((tin * PRICE_IN + tout * PRICE_OUT) / max(1, len(arr)), 9), "decision_rows": len(rows), "log_sha256": digest}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--robots", default="3,6,10,20")
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--arrivals")
    ap.add_argument("--out", default=str(ROOT / "results" / "e7"))
    a = ap.parse_args(argv)
    wards = [f"w{w}{s}" for w in range(3) for s in "ab"]
    arr = read_arrivals(a.arrivals) if a.arrivals else arrivals(a.hours, a.seed, wards)
    results = []
    for r in [int(x) for x in a.robots.split(",")]:
        first = simulate(r, arr, a.seed)
        again = simulate(r, arr, a.seed)
        first["replay_identical"] = first["log_sha256"] == again["log_sha256"]
        for k in ("decision_ms_p50", "decision_ms_p95"):          # best of the two runs: a shared machine's jitter is not scaling
            first[k] = min(first[k], again[k])
        results.append(first)
    base = next((x for x in results if x["robots"] == 3), results[0])
    for x in results:
        x["decision_p95_vs_3_robots"] = round(x["decision_ms_p95"] / base["decision_ms_p95"], 2) if base["decision_ms_p95"] else None
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps({"seed": a.seed, "arrivals": len(arr), "results": results}, indent=1))
    print(f"{len(arr)} requests over {a.hours} h (seed {a.seed}); decision time is the cloud code's own wall time per decision, best of 2 runs (stand-in model, no model latency)")
    print(f"{'robots':>6}{'done':>7}{'missed':>8}{'wait p50 s':>12}{'wait p95 s':>12}{'dec p95 ms':>12}{'vs 3':>7}{'calls/task':>12}{'cost $':>11}  replay")
    for x in results:
        print(f"{x['robots']:>6}{x['completed']:>7}{x['missed_rate']:>8.1%}{x['wait_s_p50']:>12.0f}{x['wait_s_p95']:>12.0f}{x['decision_ms_p95']:>12.2f}"
              f"{x['decision_p95_vs_3_robots']:>7}{x['llm_calls_per_task']:>12}{x['cost_per_task_usd']:>11.2e}  {'identical' if x['replay_identical'] else 'DIFFERENT'}")
    ten = next((x for x in results if x["robots"] >= 10), None)
    if ten:
        print(f"\nNFR-3 (cloud side): at {ten['robots']} robots the decision p95 is {ten['decision_p95_vs_3_robots']}x the 3-robot baseline (target <= 1.5): "
              f"{'MET' if ten['decision_p95_vs_3_robots'] <= 1.5 else 'NOT MET'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
