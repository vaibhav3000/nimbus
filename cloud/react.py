"""The cloud's reaction to what the edge reports (PRD Table 2, FR-9/FR-16).

UNEXPECTED_EVENT: record the closure on the shared map, then choose a new plan.
CLARIFY: answer from the rules or the context, or ask the requester.

The model proposes through the bounded agent loop (cloud/agent.py); code
decides what is allowed and executes. If the model is off (C3), down, or
answers outside what code accepts, hand-written rules decide the same thing,
so the edge is never left waiting (FR-15, FR-18).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta

from .agent import run_agent
from .clock import iso
from .notify import Notice
from .orchestrator_types import LLM_MODES, Outcome, row as _row
from .readtools import ReadTools


@dataclass
class Reaction:
    outcome: Outcome | None = None          # a new lease (or a failed re-plan) to apply
    rows: list = field(default_factory=list)  # decision-log rows to write before the outcome's own
    publish: list = field(default_factory=list)
    answer: str | None = None               # CLARIFY answered by the cloud
    question: dict | None = None            # CLARIFY the cloud cannot answer: ask the requester
    llm_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0


def blocked_zones(topo, payload: dict) -> list[str]:
    """Zones an UNEXPECTED_EVENT says are unusable: structured segments first,
    otherwise any known zone id (or alias) named in its 'where' text."""
    zones = [topo.resolve_node(s["segment_id"]) for s in payload.get("segments", [])
             if isinstance(s, dict) and s.get("state") in ("blocked", "closed", "crowded")]
    zones = [z for z in zones if z]
    if not zones:
        names = list(topo.nodes) + list(getattr(topo, "_alias", {}))
        zones = [topo.resolve_node(n) for n in names
                 if re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", payload.get("where", ""), re.I)]
    return sorted({z for z in zones if z})


class Reactor:
    def __init__(self, orch):
        self.o = orch

    # -- UNEXPECTED_EVENT ---------------------------------------------------------------------

    def on_event(self, task: dict, payload: dict) -> Reaction:
        o = self.o
        zones = blocked_zones(o.topo, payload)
        if payload.get("_retry") and not any(z in o.mapstate.closed(o.clock.now()) for z in zones):
            cur = o.repo.current(task["task_id"])      # a "look again" came due and the way is open again: nothing to do
            return Reaction(rows=[_row("rule", {"zones": zones}, {"decision": "react", "action": "no_action",
                                                                 "rationale": "the blockage cleared while waiting"},
                                       cur["version"] if cur else None)])
        states = {s.get("segment_id"): s.get("state") for s in payload.get("segments", []) if isinstance(s, dict)}
        for z in zones:
            o.mapstate.report(z, states.get(z, "blocked"), task.get("lease", {}).get("cluster_id", "edge"), o.clock.now())
        cur = o.repo.current(task["task_id"])
        react = Reaction()
        if cur is None or not task.get("spec"):
            return react
        if zones and set(zones) <= set(cur["forbidden_zones"]):
            react.rows.append(_row("rule", {"blocked_zones": zones}, {"decision": "react", "action": "no_action",
                                                                     "rationale": "the live lease already avoids these zones"},
                                   cur["version"]))
            return react
        robot_zone = payload.get("robot_zone") or o.fleet.robot_zone(cur["assigned_robots"][0])
        for z in zones:   # selective sharing (FR-11): tell only the clusters whose area or live leases include the zone
            for c in o.mapstate.affected_clusters(z, o.repo, o.clock.now()):
                self._notice(react, Notice("cluster", f"{z} is {states.get(z, 'blocked')}; routes through it are closed.",
                                           type="MAP", cluster_id=c), None, task)
        choice = self._choose_event(task, payload, cur, zones, robot_zone, react)
        row_out = {"decision": "react", "action": choice["action"], "source": choice["source"],
                   "rationale": choice["rationale"], "blocked_zones": zones}
        if choice.get("trace"):
            row_out["trace"] = choice["trace"]
        if choice.get("error"):
            row_out["model_error"] = choice["error"]
        react.rows.append(_row("cloud" if choice["source"] == "llm" else "rule",
                               {"what_happened": payload["what_happened"], "where": payload["where"],
                                "robot_zone": robot_zone}, row_out, cur["version"]))
        reason = payload["what_happened"]
        out = None
        if choice["action"] == "wait":     # the blockage may clear by itself: look again later (the ticker does)
            o.waits[task["task_id"]] = {"until": iso(o.clock.now() + timedelta(seconds=choice["seconds"])),
                                        "payload": dict(payload, _retry=True), "version": cur["version"]}
            react.rows.append(_row("cloud", {"zones": zones}, {"decision": "wait_and_retry", "seconds": choice["seconds"]}, cur["version"]))
        elif choice["action"] == "escalate":
            self._notice(react, Notice("operator", f"{task['task_id']}: {choice['text']}", task_id=task["task_id"],
                                       lease_version=cur["version"]), cur["version"], task)
        elif choice["action"] == "replan":
            out = o.replan(task, blocked_zones=choice["avoid"], reason=reason)
            if out.kind != "leased":   # nothing inside this cluster's area works: another cluster takes the rest (SC-2)
                react.rows += out.rows
                out = o.reassign(task, reason, robot_zone=robot_zone)
        elif choice["action"] == "hand_over":
            out = o.reassign(task, reason, only_cluster=choice["to_cluster"], robot_zone=robot_zone)
            if out.kind != "leased":
                react.rows += out.rows
                out = o.replan(task, blocked_zones=zones, reason=reason) if zones else out
        if out is not None:
            react.outcome = out
            if out.kind == "leased" and out.lease["cluster_id"] != cur["cluster_id"]:
                self._notice(react, Notice("cluster", f"{task['task_id']} was handed to {out.lease['cluster_id']}: stop and park.",
                                           type="WARNING", cluster_id=cur["cluster_id"], task_id=task["task_id"],
                                           lease_version=out.lease["version"]), out.lease["version"], task)
                self._notice(react, Notice("operator", f"{task['task_id']} re-assigned from {cur['cluster_id']} to "
                                                       f"{out.lease['cluster_id']} ({reason}).", task_id=task["task_id"]),
                             out.lease["version"], task)
            if out.kind != "leased":
                react.rows.append(_row("rule", {"reason": out.reason}, {"decision": "alert_operator",
                                                                       "text": f"re-plan failed: {out.reason}"}, cur["version"]))
                self._notice(react, Notice("operator", f"{task['task_id']}: re-plan failed ({out.reason}). Operator decision needed.",
                                           task_id=task["task_id"], lease_version=cur["version"]), cur["version"], task)
        return react

    def _choose_event(self, task, payload, cur, zones, robot_zone, react: Reaction) -> dict:
        o = self.o
        fallback = {"action": "replan" if zones else "no_action", "avoid": zones, "source": "rules",
                    "rationale": "rules: route around the reported zones" if zones else "rules: no zone named, nothing to avoid"}
        if o.mode not in LLM_MODES:
            return fallback
        ctx = o.build_context(payload["what_happened"], task["task_id"], [])
        options = o.preview_options(task, zones, robot_zone)
        user = (f"UNEXPECTED_EVENT: {payload['what_happened']} at {payload['where']}; robots saw: {payload['robots_saw']}\n"
                f"BLOCKED_ZONES: {zones}\nROBOT_ZONE: {robot_zone}\n"
                f"LEASE: allowed={cur['allowed_zones']} forbidden={cur['forbidden_zones']} cluster={cur['cluster_id']}\n"
                f"OPTIONS (already computed by code): {options}")

        def validate(call: dict) -> list[str]:
            if call["name"] == "hand_over":
                t = call["input"]["to_cluster"]
                return [] if t in o.fleet.clusters and t != cur["cluster_id"] else [f"{t!r} is not another cluster"]
            if call["name"] != "replan":
                return []
            bad = [z for z in call["input"]["avoid_zones"] if o.topo.resolve_node(z) is None]
            return [f"unknown zones {bad}"] if bad else []

        res = run_agent(o.llm, system=o.pack.prompts["react"] + "\n\n" + ctx.to_prompt(), user=user,
                        tools=o.tools.names("react_event"), registry=o.tools, read=self._read(),
                        terminal={"replan", "hand_over", "wait_and_retry", "escalate_operator", "no_action"}, validate=validate, context=ctx, fleet_view=o.view(task["task_id"]))
        react.llm_calls += res.calls
        react.tokens_in += res.tokens_in
        react.tokens_out += res.tokens_out
        if res.call is None:
            return {**fallback, "error": res.error, "trace": res.trace}
        i = res.call["input"]
        if res.call["name"] == "no_action" and zones:
            return {**fallback, "error": "model chose no_action although zones were reported", "trace": res.trace}
        if res.call["name"] == "no_action":
            return {"action": "no_action", "avoid": [], "source": "llm", "rationale": i["rationale"], "trace": res.trace}
        if res.call["name"] == "wait_and_retry":
            return {"action": "wait", "seconds": i["seconds"], "avoid": zones, "source": "llm", "rationale": i["rationale"], "trace": res.trace}
        if res.call["name"] == "escalate_operator":
            return {"action": "escalate", "text": i["text"], "avoid": zones, "source": "llm",
                    "rationale": i.get("rationale") or i["text"], "trace": res.trace}
        if res.call["name"] == "hand_over":
            return {"action": "hand_over", "to_cluster": i["to_cluster"], "avoid": zones, "source": "llm",
                    "rationale": i["rationale"], "trace": res.trace}
        avoid = sorted({*zones, *(o.topo.resolve_node(z) for z in i["avoid_zones"])})
        return {"action": "replan", "avoid": avoid, "source": "llm", "rationale": i["rationale"], "trace": res.trace}

    # -- CLARIFY ----------------------------------------------------------------------------------

    def on_clarify(self, task: dict, payload: dict, version: int) -> Reaction:
        o = self.o
        react = Reaction()
        choice = {"name": "ask_requester", "input": {"question": payload["question"],
                                                     "possible_readings": payload["possible_readings"]},
                  "source": "rules"}
        if o.mode in LLM_MODES:
            ctx = o.build_context(payload["question"], task["task_id"], [])
            user = f"CLARIFY from the edge: {payload['question']}\nPOSSIBLE_READINGS: {payload['possible_readings']}"
            res = run_agent(o.llm, system=o.pack.prompts["react"] + "\n\n" + ctx.to_prompt(), user=user,
                            tools=o.tools.names("react_clarify"), registry=o.tools, read=self._read(),
                            terminal={"answer_clarification", "ask_requester"}, context=ctx, fleet_view=o.view(task["task_id"]))
            react.llm_calls, react.tokens_in, react.tokens_out = res.calls, res.tokens_in, res.tokens_out
            if res.call is not None:
                choice = {**res.call, "source": "llm", "trace": res.trace}
        i = choice["input"]
        out = {"decision": "answer_clarify" if choice["name"] == "answer_clarification" else "ask_requester",
               "source": choice["source"], **i, **({"trace": choice["trace"]} if choice.get("trace") else {})}
        react.rows.append(_row("cloud" if choice["source"] == "llm" else "rule",
                               {"question": payload["question"], "possible_readings": payload["possible_readings"]}, out, version))
        if choice["name"] == "answer_clarification":
            react.answer = i["answer"]
            cluster = (task.get("lease") or {}).get("cluster_id")
            if cluster:
                self._notice(react, Notice("cluster", i["answer"], type="ANSWER", cluster_id=cluster,
                                           task_id=task["task_id"], lease_version=version), version, task)
        else:
            react.question = {"question": i["question"], "possible_readings": i["possible_readings"]}
        return react

    # -- helpers ------------------------------------------------------------------------------------

    def _notice(self, react: Reaction, n: Notice, version: int | None, task: dict) -> None:
        entry, pub = self.o.notifier.send(n)
        react.rows.append(_row("cloud", {"task_id": task["task_id"]},
                               {"decision": "notify", "audience": n.audience, "type": n.type, "text": n.text,
                                "cluster_id": n.cluster_id, "message_id": entry["message_id"]}, version))
        if pub:
            react.publish.append(pub)

    def _read(self) -> ReadTools:
        o = self.o
        return ReadTools(topo=o.topo, fleet=o.fleet, policies=o.policies, mapstate=o.mapstate, clock=o.clock,
                         load_task=o.load_task, repo=o.repo)


def _scoped_react(fn):
    import functools
    from .trace import task_scope

    @functools.wraps(fn)
    def inner(self, task, *a, **k):
        with task_scope(task.get("task_id")):
            return fn(self, task, *a, **k)
    return inner


Reactor.on_event = _scoped_react(Reactor.on_event)
Reactor.on_clarify = _scoped_react(Reactor.on_clarify)
