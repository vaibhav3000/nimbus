#!/usr/bin/env python3
"""T8 -- 3 simultaneous requests, 2 available robots: EDF vs FIFO (two C3 implementations).

Why a simulation and not Gazebo: the deadline gap only appears when two tasks are
queued and compete for one freed robot. That needs controlled service times and a
controlled clock, which a live Gazebo run cannot reproduce reliably. This drives the
REAL cloud (Orchestrator: intent, classical allocator, rule checker, lease repo,
scheduler, drain) on a simulated clock -- only robot travel is modelled, exactly as
experiments/cloud/c5_scheduling.py does. Nothing about scheduling is re-implemented.

Scenario (identical for both schedulers):
  * 2 robots available (one per wing; the rest reported failed).
  * 3 requests to the SAME east ward (2B) arrive together: two routine, then one
    urgent. One leases immediately (the single east robot); the other two queue.
  * When the east robot frees, the scheduler chooses who goes next:
      - EDF : the urgent request -> it still makes its deadline.
      - FIFO (naive C3): the routine that arrived first -> the urgent waits for the
        robot to free a SECOND time and misses its deadline.

This is the evidence for "a classical baseline's success under contention depends on
how well it is engineered (EDF vs FIFO), which motivates the planning the LLM adds."

    python3 scripts/run_t8.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("NIMBUS_PACK", "hospital_sim6")
os.environ.setdefault("NIMBUS_LLM", "fake")
os.environ.setdefault("NIMBUS_MODE", "C3")      # classical baseline: the only variable is the scheduler
os.environ.setdefault("NIMBUS_BACKEND", "local")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "experiments" / "cloud"))

import c5_scheduling as c5   # noqa: E402  (reuse its make_orch / lease_duration / beat)

URGENT_DEADLINE_MIN = 13     # the pack's default urgent deadline
# Per-delivery service time. The graph route-length estimate (c5.lease_duration)
# undercounts badly -- it ignores acceleration, cornering and the pick/photo steps;
# the measured Gazebo mission was ~11 min round-trip, so a one-way delivery leg is
# ~5 min. Override via T8_SERVICE_MIN. (With a 12-min urgent deadline the scheduler
# gap is visible for any 4 < service <= 6 min: EDF serves the urgent in time, one
# FIFO inversion pushes it past the deadline.)
SERVICE_MIN = float(os.environ.get("T8_SERVICE_MIN", "5"))


def run(scheduler: str) -> dict:
    orch = c5.make_orch(scheduler, robots_up=2)     # 2 robots (1 east, 1 west); the rest failed
    start = orch.clock.now()
    tasks: dict[str, dict] = {}

    def now_s() -> float:
        return (orch.clock.now() - start).total_seconds()

    def submit(tid: str, text: str) -> None:
        task = {"task_id": tid, "request_text": text, "status": "received"}
        tasks[tid] = task
        out = orch.handle_request(task, text)
        task["spec"] = out.spec
        task["priority"] = (out.spec or {}).get("priority")
        if out.kind == "leased":
            task.update(status="leased", lease=out.lease, leased_at=now_s(),
                        dur=SERVICE_MIN * 60.0)
        else:
            task["status"] = out.kind

    def lease_from_queue() -> None:
        for qt, out in orch.drain(lambda tid: tasks.get(tid)):
            qt.update(status="leased", lease=out.lease, leased_at=now_s(),
                      dur=SERVICE_MIN * 60.0)

    def finish(tid: str) -> None:
        t = tasks[tid]
        orch.clock.advance(t["leased_at"] + t["dur"] - now_s())   # jump to when this lease ends
        c5.beat(orch)
        t.update(status="completed", completed_at=now_s())
        orch.finish(t)
        lease_from_queue()

    # Three requests to the same east ward, arriving together: routine, routine, urgent.
    submit("t1-routine", "a wheelchair to 2B")
    submit("t2-routine", "a blanket to 2B")
    submit("t3-urgent", "a monitor to 2B, urgent")

    # Drive completions until the urgent one has run (or we run out of work).
    guard = 0
    while tasks["t3-urgent"]["status"] != "completed" and guard < 10:
        running = [t for t in tasks.values() if t["status"] == "leased"]
        if not running:
            break
        finish(min(running, key=lambda t: t["leased_at"] + t["dur"])["task_id"])
        guard += 1

    u = tasks["t3-urgent"]
    done_min = (u.get("completed_at", float("inf")) / 60.0) if u["status"] == "completed" else float("inf")
    return {
        "scheduler": scheduler,
        "served_first_from_queue": "t3-urgent" if _first_queue_lease(tasks) == "t3-urgent" else "t2-routine",
        "urgent_completed_min": done_min,
        "deadline_min": URGENT_DEADLINE_MIN,
        "urgent_meets_deadline": done_min <= URGENT_DEADLINE_MIN,
    }


def _first_queue_lease(tasks: dict) -> str:
    # Of the two that started queued, whichever leased earlier.
    queued = [t for t in (tasks["t2-routine"], tasks["t3-urgent"]) if "leased_at" in t]
    return min(queued, key=lambda t: t["leased_at"])["task_id"] if queued else "-"


def main() -> int:
    print("T8: 3 requests to ward 2B, 2 robots, urgent deadline = "
          f"{URGENT_DEADLINE_MIN} min\n")
    rows = [run("edf"), run("fifo")]
    print(f"{'scheduler':<10}{'served next after free':<26}{'urgent done (min)':<20}{'deadline':<10}verdict")
    for r in rows:
        done = "never" if r["urgent_completed_min"] == float("inf") else f"{r['urgent_completed_min']:.1f}"
        verdict = "MEETS" if r["urgent_meets_deadline"] else "MISSES"
        label = "EDF  (PRD C3)" if r["scheduler"] == "edf" else "FIFO (naive C3)"
        print(f"{label:<16}{r['served_first_from_queue']:<22}{done:<20}{r['deadline_min']:<10}{verdict}")
    edf, fifo = rows
    ok = edf["urgent_meets_deadline"] and not fifo["urgent_meets_deadline"]
    print("\n" + ("T8 CONFIRMED: EDF serves the urgent request first and meets its deadline; "
                  "FIFO serves the routine first and the urgent misses."
                  if ok else
                  "Gap not separated at these service times -- inspect the numbers above."))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
