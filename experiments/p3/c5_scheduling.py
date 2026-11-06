"""C5 - Priority scheduling (FR-14, T8).

A discrete-event simulation that drives the REAL cloud (Orchestrator: intent, allocator, rule checker, lease
repository, scheduler, drain) on a simulated clock. Nothing about scheduling is re-implemented here; only the
robots' travel is simulated: a lease finishes after (route length / lease speed limit) + the template's waits, then
the edge's OUTPUT_READY is applied (orch.finish) and the queue is drained, exactly as the server does.

Workload: Poisson arrivals (urgent 20%, routine 80%) to 2B, 2C and ward 3, 2 hours of simulated time per run, with
k = 2, 4 or 6 working robots (the others are reported failed) and offered load rho = 0.3 .. 0.9, where
rho = arrival rate x mean service time / k (mean service time measured from the leases themselves in a warm-up).
Schedulers: `ours` (cloud/scheduler.py: priority rank, then earliest deadline, then arrival) vs `fifo` (arrival
order only), same seeds, same arrivals.

Checks (FR-14): queue inversions = a routine task given a robot from the queue while an urgent task that the same
cluster could serve was waiting (must be 0 for ours). Also reported: waiting time and deadline-miss ratio by class,
and T8 (3 simultaneous requests, 2 robots).

    python experiments/p3/c5_scheduling.py --seeds 10
"""

from __future__ import annotations

import argparse
import heapq
import os
import random
import sys
import tempfile
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

os.environ["NIMBUS_PACK"] = "hospital_sim6"
os.environ["NIMBUS_LLM"] = "fake"
os.environ["NIMBUS_MODE"] = "C1"
sys.path.insert(0, str(lib.ROOT))

from cloud.allocator import eligible_options  # noqa: E402
from cloud.clock import FixedClock  # noqa: E402
from cloud.orchestrator import Orchestrator  # noqa: E402
from cloud.scheduler import Scheduler  # noqa: E402

PLACES = [("2B", "east_waiting_approach"), ("2C", "west_waiting_approach"), ("ward 3", "south_room_approach")]
ITEMS = ["a wheelchair", "a blanket", "a monitor", "an IV pole"]
SIM_S = 2 * 3600


class FifoScheduler(Scheduler):
    """Baseline: arrival order only (no priority, no deadline)."""

    def ordered(self):
        with self._lock:
            return sorted(self._entries.values(), key=lambda e: e.seq)


def make_orch(scheduler: str, robots_up: int) -> Orchestrator:
    orch = Orchestrator.from_env(Path(tempfile.mkdtemp(prefix="c5-")), clock=FixedClock(), backend="local")
    if scheduler == "fifo":
        orch.scheduler = FifoScheduler(orch.pack.ranks)
    all_robots = [(c, r) for c in sorted(orch.fleet.clusters) for r in sorted(orch.fleet.clusters[c]["robots"])]
    # keep `robots_up` robots working, spread over both clusters; the rest are reported failed
    keep = set()
    by_c = defaultdict(list)
    for c, r in all_robots:
        by_c[c].append(r)
    i = 0
    while len(keep) < robots_up:
        c = sorted(by_c)[i % len(by_c)]
        if by_c[c]:
            keep.add(by_c[c].pop(0))
        i += 1
    orch._c5_keep = keep
    for c in orch.fleet.clusters:      # plenty of stock: this experiment is about robots and order, not about running out
        orch.fleet.set_stock(c, {i["id"]: 100000 for i in orch.pack.items})
    beat(orch)
    return orch


def beat(orch):
    for c in orch.fleet.clusters:
        orch.fleet.heartbeat(c, {"robots": [{"id": r, "state": "idle" if r in orch._c5_keep else "failed"}
                                            for r in orch.fleet.clusters[c]["robots"]]})


def lease_duration(orch, lease) -> float:
    edges = {}
    for a, b, w in orch.pack.topology_doc["edges"]:
        edges[(a, b)] = edges[(b, a)] = float(w)
    speed = float(lease.get("speed_limit") or 0.5)
    total = 0.0
    for s in lease["task_sequence"]:
        p = s.get("params", {})
        if s["cmd"] == "go_to":
            via = p.get("via") or []
            total += sum(edges.get((x, y), 0.0) for x, y in zip(via, via[1:])) / speed
        elif s["cmd"] == "wait":
            total += float(p.get("seconds", 0))
    return total


def run(scheduler: str, robots_up: int, rho: float, seed: int, mean_service: float, rd=None, gantt=False) -> list[dict]:
    rng = random.Random(seed * 1000 + robots_up * 10 + int(rho * 100))
    orch = make_orch(scheduler, robots_up)
    lam = rho * robots_up / mean_service
    tasks: dict[str, dict] = {}
    events = []            # (time, seq, kind, payload)
    seq = 0
    t = 0.0
    while True:
        t += rng.expovariate(lam)
        if t > SIM_S:
            break
        pri = "urgent" if rng.random() < 0.2 else "routine"
        place, _ = rng.choice(PLACES)
        text = f"Deliver {rng.choice(ITEMS)} to {place}" + (", urgent." if pri == "urgent" else ", routine.")
        heapq.heappush(events, (t, seq, "arrive", text)); seq += 1
    log = []
    n = 0
    start = orch.clock.now()

    def now_s():
        return (orch.clock.now() - start).total_seconds()

    def leased(task, out):
        task.update(status="leased", lease=out.lease, spec=out.spec, reservation=out.reservation, leased_at=now_s())
        dur = lease_duration(orch, out.lease)
        task["duration"] = dur
        nonlocal seq
        heapq.heappush(events, (now_s() + dur, seq, "done", task["task_id"])); seq += 1

    def queue_snapshot():
        return [tasks[e.task_id] for e in orch.scheduler.ordered() if e.task_id in tasks and tasks[e.task_id]["status"] == "queued"]

    while events:
        t, _, kind, payload = heapq.heappop(events)
        orch.clock.advance(t - now_s())
        beat(orch)
        if kind == "arrive":
            n += 1
            task = {"task_id": f"task-{n:06d}", "request_text": payload, "status": "received", "arrived": t}
            tasks[task["task_id"]] = task
            out = orch.handle_request(task, payload)
            task["spec"], task["reservation"] = out.spec, out.reservation
            task["priority"] = (out.spec or {}).get("priority")
            task["deadline_min"] = (out.spec or {}).get("deadline_minutes")
            task["dest"] = (out.spec or {}).get("destination")
            if out.kind == "leased":
                task["from_queue"] = False
                leased(task, out)
            else:
                task["status"] = out.kind
                task["why_waiting"] = out.reason
        else:
            task = tasks[payload]
            task["status"], task["completed_at"] = "completed", t
            orch.finish(task)
            waiting = queue_snapshot()
            placeable = {w["task_id"] for w in waiting if w.get("priority") == "urgent"
                         and eligible_options(w["spec"], orch.view(w["task_id"]))[0]}
            drained = orch.drain(lambda tid: tasks.get(tid))
            for qt, out in drained:
                qt["from_queue"] = True
                leased(qt, out)
            # inversion: a routine task got a robot from the queue while an urgent task that cluster could serve
            # was waiting before AND is still waiting after this drain
            for qt, out in drained:
                if qt.get("priority") != "routine":
                    continue
                zones = orch.topo.cluster_zones(out.lease["cluster_id"])
                still = [w for w in waiting if w["status"] == "queued" and w["task_id"] in placeable
                         and orch.topo.anchor_of(w["dest"]) in zones]
                if still:
                    qt["inversion"] = True
                    qt["inversion_detail"] = [{"urgent": w["task_id"], "reason": w.get("why_waiting")} for w in still]
    for task in tasks.values():
        if "arrived" not in task:
            continue
        end = task.get("completed_at")
        dl = task["arrived"] + (task.get("deadline_min") or 60) * 60
        rec = {"scheduler": scheduler, "robots": robots_up, "rho": rho, "seed": seed, "task": task["task_id"],
               "priority": task.get("priority"), "dest": task.get("dest"), "status": task["status"],
               "arrived_s": round(task["arrived"], 1), "wait_s": round(max(0.0, task["leased_at"] - task["arrived"]), 1) if "leased_at" in task else None,
               "completed_s": round(end, 1) if end else None, "deadline_met": bool(end and end <= dl),
               "missed": (end is None and SIM_S > dl) or bool(end and end > dl), "from_queue": task.get("from_queue"),
               "inversion": task.get("inversion", False), "inversion_detail": task.get("inversion_detail"), "cluster": (task.get("lease") or {}).get("cluster_id"),
               "robot": ((task.get("lease") or {}).get("assigned_robots") or [None])[0], "duration_s": round(task.get("duration", 0), 1)}
        log.append(rec)
        if rd is not None:
            rd.raw(f"tasks_{scheduler}", rec)
    return log


def t8(rd) -> dict:
    """T8: 3 simultaneous requests (2 routine first, then 1 urgent), 2 working robots in the east wing."""
    res = {}
    for sched in ("ours", "fifo"):
        orch = Orchestrator.from_env(Path(tempfile.mkdtemp(prefix="c5t8-")), clock=FixedClock(), backend="local")
        if sched == "fifo":
            orch.scheduler = FifoScheduler(orch.pack.ranks)
        orch._c5_keep = {"R1", "R2"}
        beat(orch)
        texts = ["Deliver a wheelchair to 2B, routine.", "Deliver a blanket to 2B, routine.",
                 "Deliver a monitor to 2B, routine.", "Deliver an IV pole to 2B, urgent."]
        tasks, outs = [], []
        for i, text in enumerate(texts):
            task = {"task_id": f"task-{i + 1:06d}", "request_text": text, "status": "received"}
            out = orch.handle_request(task, text)
            task.update(status=out.kind, lease=out.lease, spec=out.spec, reservation=out.reservation)
            tasks.append(task); outs.append(out.kind)
        first = next(t for t in tasks if t["status"] == "leased")
        first["status"] = "completed"
        orch.finish(first)
        got = [t["task_id"] for t, o in orch.drain(lambda tid: next((x for x in tasks if x["task_id"] == tid), None))]
        res[sched] = {"initial": outs, "next_served_after_a_robot_frees": got,
                      "urgent_served_next": got[:1] == ["task-000004"]}
    rd.json("t8.json", res)
    return res


def plots(rd, rows) -> list[str]:
    plt = lib.plt()
    names = []
    fig, ax = plt.subplots()
    sel = [r for r in rows if r["robots"] == 4 and r["rho"] == 0.9 and r["wait_s"] is not None]
    for sched, ls in (("ours", "-"), ("fifo", "--")):
        for pri in ("urgent", "routine"):
            lib.ecdf(ax, [r["wait_s"] / 60 for r in sel if r["scheduler"] == sched and r["priority"] == pri],
                     f"{pri}, {sched}", color=lib.COLORS[pri])
            ax.lines[-1].set_linestyle(ls)
    ax.set_xlabel("wait from request to lease (min), 4 robots, load 0.9"); ax.set_ylabel("share of tasks")
    ax.set_title("C5: waiting time by priority class"); ax.legend(fontsize=8)
    names.append(lib.save(fig, rd, "wait_cdf_by_priority"))
    fig, axes = plt.subplots(1, 2, figsize=(9, 3.6), sharey=True)
    for ax, pri in zip(axes, ("urgent", "routine")):
        for sched, ls in (("ours", "-"), ("fifo", "--")):
            for k, mk in ((2, "o"), (4, "s"), (6, "^")):
                xs = sorted({r["rho"] for r in rows})
                ys = []
                for x in xs:
                    sub = [r for r in rows if r["scheduler"] == sched and r["robots"] == k and r["rho"] == x and r["priority"] == pri]
                    ys.append(sum(r["missed"] for r in sub) / len(sub) if sub else None)
                ax.plot(xs, ys, ls=ls, marker=mk, color=lib.COLORS[sched], label=f"{sched}, {k} robots", ms=4)
        ax.set_title(f"{pri} tasks"); ax.set_xlabel("offered load (rho)")
    axes[0].set_ylabel("deadline-miss ratio"); axes[1].legend(fontsize=7)
    fig.suptitle("C5: missed deadlines vs load, our scheduler vs first-come-first-served", y=1.04)
    names.append(lib.save(fig, rd, "miss_ratio_vs_load"))
    # Gantt of one run
    g = [r for r in rows if r["scheduler"] == "ours" and r["robots"] == 4 and r["rho"] == 0.9 and r["seed"] == 1 and r["robot"]]
    g = [r for r in g if r["arrived_s"] < 3600]
    fig, ax = plt.subplots(figsize=(9, 3))
    robots = sorted({r["robot"] for r in g})
    for r in g:
        y = robots.index(r["robot"])
        start = r["arrived_s"] + (r["wait_s"] or 0)
        ax.barh(y, r["duration_s"] / 60, left=start / 60, color=lib.COLORS[r["priority"]], edgecolor="white", height=0.6)
    ax.set_yticks(range(len(robots)), robots); ax.set_xlabel("simulated time (min)")
    ax.set_title("C5: robot schedule, first hour (red urgent, blue routine), 4 robots, load 0.9")
    names.append(lib.save(fig, rd, "robot_gantt"))
    return names


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seeds", type=int, default=10)
    ap.add_argument("--robots", type=int, nargs="+", default=[2, 4, 6])
    ap.add_argument("--loads", type=float, nargs="+", default=[0.3, 0.5, 0.7, 0.9])
    ap.add_argument("--replot", action="store_true", help="redraw the plots from results/final/C5/raw without re-running")
    a = ap.parse_args(argv)
    if a.replot:
        import json
        rd = lib.ResultDir("C5", "Priority scheduling (FR-14, T8)", None)
        import gzip
        files = sorted((rd.dir / "raw").glob("tasks_*.jsonl")) + sorted((rd.dir / "raw").glob("tasks_*.jsonl.gz"))
        rows = [json.loads(line) for f in files for line in (gzip.open(f, "rt") if f.suffix == ".gz" else f.open())]
        print("redrew", plots(rd, rows))
        return 0
    rd = lib.ResultDir("C5", "Priority scheduling (FR-14, T8)", None)
    rd.env.update({"source": "measured", "target": "in-process cloud on a simulated clock (pack hospital_sim6)", "model": "rule-based-stand-in (intent only)"})
    rd.reset_raw()
    # mean service time from the cloud's own leases (warm-up at light load)
    warm = run("ours", 6, 0.2, 999, 300.0)
    durs = [r["duration_s"] for r in warm if r["duration_s"]]
    mean_service = sum(durs) / len(durs)
    rd.env["mean_service_s"] = round(mean_service, 1)
    rows = []
    for sched in ("ours", "fifo"):
        for k in a.robots:
            for rho in a.loads:
                for seed in range(1, a.seeds + 1):
                    rows += run(sched, k, rho, seed, mean_service, rd)
            print(f"[C5] {sched} {k} robots done ({len(rows)} task records)")
    t8res = t8(rd)
    summary = []
    for sched in ("ours", "fifo"):
        for k in a.robots:
            for rho in a.loads:
                for pri in ("urgent", "routine"):
                    sub = [r for r in rows if r["scheduler"] == sched and r["robots"] == k and r["rho"] == rho and r["priority"] == pri]
                    w = [r["wait_s"] for r in sub if r["wait_s"] is not None]
                    summary.append({"scheduler": sched, "robots": k, "rho": rho, "priority": pri, "tasks": len(sub),
                                    "wait_p50_s": lib.r(lib.percentile(w, 50), 1), "wait_p95_s": lib.r(lib.percentile(w, 95), 1),
                                    "miss_ratio": lib.r(sum(r["missed"] for r in sub) / len(sub)) if sub else None,
                                    "queue_inversions": sum(r["inversion"] for r in sub), "source": "measured"})
    rd.summary(summary)
    names = plots(rd, rows)
    import gzip
    import shutil
    for f in (rd.dir / "raw").glob("tasks_*.jsonl"):     # 100k+ task records: keep them compressed in the repository
        with f.open("rb") as src, gzip.open(f.with_suffix(".jsonl.gz"), "wb") as dst:
            shutil.copyfileobj(src, dst)
        f.unlink()
    inv_ours = sum(r["inversion"] for r in rows if r["scheduler"] == "ours")
    inv_fifo = sum(r["inversion"] for r in rows if r["scheduler"] == "fifo")
    hi = [s for s in summary if s["rho"] == max(a.loads) and s["robots"] == 4]
    miss = {(s["scheduler"], s["priority"]): s["miss_ratio"] for s in hi}
    ok = inv_ours == 0 and t8res["ours"]["urgent_served_next"]
    rd.finish(claim="Urgent work goes first: the queue never gives a robot to a routine task while an urgent one it could serve is waiting.",
              pass_bar="An urgent task never waits behind a routine one in the queue (FR-14); T8 handled.",
              verdict="PASS" if ok else "FAIL",
              measured=(f"queue inversions: ours {inv_ours}, first-come-first-served {inv_fifo} over {len(rows)} tasks "
                        f"({a.seeds} seeds x {len(a.robots)} fleet sizes x {len(a.loads)} loads x 2 h); at load {max(a.loads)} with 4 robots "
                        f"urgent miss ratio ours {miss.get(('ours', 'urgent'))} vs fifo {miss.get(('fifo', 'urgent'))}, routine "
                        f"{miss.get(('ours', 'routine'))} vs {miss.get(('fifo', 'routine'))}; T8: urgent served next = {t8res['ours']['urgent_served_next']}"),
              notes=[f"Mean service time {mean_service:.0f} s per delivery, measured from the cloud's leases (route length / lease speed + waits).",
                     "The cloud does not preempt a robot already on a routine lease; an urgent task arriving when every robot is busy "
                     "waits for the next robot to free, then goes first. Preemption is not a PRD requirement.",
                     "Robot travel is simulated (no edge, no Gazebo); every allocation, check, lease and queue decision is the real cloud code."],
              plots=names)
    return 0


if __name__ == "__main__":
    sys.exit(main())
