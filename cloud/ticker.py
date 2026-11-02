"""Time-driven work the cloud must do even when nothing arrives (PRD FR-6, FR-12, FR-14, FR-17):

* a closure whose time is up reopens (and the clusters that were told are told again);
* a lease that ran out with no completion and no renewal ends the task's claim and puts it back in the queue;
* a deadline that is about to be, or has been, missed raises an alarm once;
* a cluster that has been silent while holding live leases is alarmed and its tasks are moved to another cluster
  when that is possible;
* queued work is retried (cheaply: only when capacity really changed).

`tick(tasks)` is a plain function of the clock and the current state, so every behaviour is testable with a fixed clock.
The server calls it from a small thread every few seconds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from .clock import iso, parse_iso
from .leases import _task_of
from .notify import Notice
from .orchestrator_types import Outcome, row as _row

AT_RISK_FRACTION = 0.8


@dataclass
class TickResult:
    apply: list = field(default_factory=list)     # [(task, Outcome)] to persist with the usual path
    touched: list = field(default_factory=list)   # tasks whose flags changed and must be saved
    notes: list = field(default_factory=list)     # human-readable lines (for logs and tests)


class Ticker:
    def __init__(self, orch):
        self.o = orch
        self._alarmed: set = set()      # (kind, key) already raised: each alarm fires once

    def _once(self, kind: str, key) -> bool:
        if (kind, key) in self._alarmed:
            return False
        self._alarmed.add((kind, key))
        return True

    def _say(self, res: TickResult, n: Notice, task: dict | None = None) -> None:
        entry, pub = self.o.notifier.send(n)
        if pub:
            self.o.publish(*pub)
        res.notes.append(n.text)

    def tick(self, tasks: list[dict]) -> TickResult:
        o, now, res = self.o, self.o.clock.now(), TickResult()
        by_id = {t["task_id"]: t for t in tasks}
        o.health_check()                                  # probes clusters that went quiet

        for zone in o.mapstate.expire(now):               # 1. closures reopen
            for c in o.mapstate.affected_clusters(zone, o.repo, now):
                self._say(res, Notice("cluster", f"{zone} has reopened.", type="MAP", cluster_id=c))
            self._say(res, Notice("operator", f"{zone} reopened (the closure report timed out)."))

        for lease in o.repo.live_leases():                # 2. leases that ran out
            task = by_id.get(_task_of(lease))
            if task is None or task["status"] != "leased" or parse_iso(lease["expiry_ts"]) > now:
                continue
            if not self._once("expired", lease["lease_id"]):
                continue
            out = o.expire(task, f"lease {lease['lease_id']} ended with no completion and no renewal")
            if out.kind == "held":
                task["status"], task["lease"] = "queued", out.lease     # so the drain below sees it as queued, not as finished
                res.apply.append((task, out))
                self._say(res, Notice("operator", f"{task['task_id']}: lease expired with no news from {lease['cluster_id']}; "
                                                  "the task is back in the queue.", task_id=task["task_id"]), task)
                self._say(res, Notice("requester", f"{task['task_id']} is delayed; it is being re-planned.", task_id=task["task_id"]), task)

        for task in tasks:                                # 3. deadlines
            spec = task.get("spec")
            if not spec or task["status"] in ("completed", "cancelled", "rejected") or not spec.get("requested_at"):
                continue
            start = parse_iso(spec["requested_at"])
            span = timedelta(minutes=spec["deadline_minutes"])
            if now > start + span and not task.get("deadline_missed"):
                task["deadline_missed"] = True
                res.touched.append(task)
                o.fleet  # no-op reference keeps the dependency obvious
                self._say(res, Notice("operator", f"{task['task_id']} missed its deadline ({task['status']}).", task_id=task["task_id"]), task)
                self._say(res, Notice("requester", f"{task['task_id']} is running late.", task_id=task["task_id"]), task)
            elif (now > start + span * AT_RISK_FRACTION and not task.get("deadline_at_risk")
                  and task["status"] in ("queued", "needs_clarification", "received")):
                task["deadline_at_risk"] = True
                res.touched.append(task)
                self._say(res, Notice("operator", f"{task['task_id']} is still {task['status']} with under 20% of its time left.",
                                      task_id=task["task_id"]), task)

        for cid in sorted(o.fleet.clusters):              # 4. silent clusters holding live leases (FR-12)
            if o.fleet.health(cid) != "down":
                continue
            held = [l for l in o.repo.live_leases() if l["cluster_id"] == cid and not _is_stop(l)]
            for lease in held:
                task = by_id.get(_task_of(lease))
                if task is None or not self._once("silent", (lease["lease_id"])):
                    continue
                out = o.reassign(task, f"{cid} has gone silent")
                if out.kind == "leased":
                    res.apply.append((task, out))
                    self._say(res, Notice("operator", f"{task['task_id']} moved from silent {cid} to {out.lease['cluster_id']}.",
                                          task_id=task["task_id"]), task)
                else:
                    res.apply.append((task, Outcome("replan_failed", reason=out.reason, rows=out.rows)))
                    self._say(res, Notice("operator", f"{cid} is silent and {task['task_id']} could not be moved: {out.reason}",
                                          task_id=task["task_id"]), task)

        res.apply += o.drain(lambda tid: by_id.get(tid))   # 5. queued work (only retried if capacity changed)
        return res


def _is_stop(lease: dict) -> bool:
    return [s["cmd"] for s in lease["task_sequence"]] == ["stop"]
