"""Survey tasks (PRD T7, "inventory sweep"): robots go and look instead of carrying. A new task type added as a pack template
and this module, with the lease schema unchanged (NFR-7). The lease can name several robots, each with its own places; the
edge filters what it saw and returns a short list in OUTPUT_READY; the cloud merges the reports into its picture (FR-17).
"""

from __future__ import annotations

import json

from . import leasebuilder, nlp
from .checker import Proposal, check_survey
from .clock import iso
from .leases import Conflict, claim_keys_for
from .orchestrator_types import Outcome, row as _row

MAX_ROBOTS = 2       # a sweep uses at most this many robots of a cluster


class Surveyor:
    def __init__(self, orch):
        self.o = orch

    def plan(self, payload: dict, text: str) -> list[dict]:
        """One survey spec per cluster that covers some of the places (a shared place goes to the first cluster that covers it)."""
        o = self.o
        p = nlp.parse(o.pack, o.topo, text)
        priority = payload.get("priority") or p.priority
        minutes = payload.get("deadline_minutes") or p.deadline_minutes
        kinds = set(o.pack.ontology.get("survey_kinds", []))
        targets = [o.topo.anchor_of(z) or z for z in payload.get("places") or []]
        targets = [t for t in dict.fromkeys(targets) if t in o.topo.nodes] or \
                  [n for n, d in sorted(o.topo.nodes.items()) if d["kind"] in kinds]
        specs, remaining = [], list(targets)
        for cid in sorted(o.fleet.clusters):
            zones = o.topo.cluster_zones(cid)
            mine = [t for t in remaining if t in zones]
            if mine:
                specs.append({"task": "survey", "item": payload["item"], "targets": mine, "priority": priority,
                              "deadline_minutes": minutes, "cluster_hint": cid, "destination": mine[0],
                              "destination_anchor": mine[0], "constraints": [], "rationale": payload.get("rationale", "inventory sweep")})
                remaining = [t for t in remaining if t not in mine]
        return specs

    def allocate(self, task: dict, spec: dict, ctx, info: dict, notice, *, request_text: str, retry: bool = False) -> Outcome:
        o, tid = self.o, task["task_id"]
        view = o.view(tid)
        cid = spec["cluster_hint"]
        cluster = o.fleet.clusters[cid]
        free = [r for r, v in sorted(cluster["robots"].items()) if v["state"] != "failed" and view.robot_free(r)][:MAX_ROBOTS]
        out_info = {"tokens_in": info["input_tokens"], "tokens_out": info["output_tokens"], "llm_calls": info["calls"], "notice": notice}
        if not free:
            return self._wait(task, spec, f"{cid}: no free robot", request_text, ctx, retry, out_info)
        prop = Proposal(cid, free, "survey: nearest-first split over the free robots", "classical")
        res = check_survey(spec, prop, view)
        if not res.ok:
            transient = any(v["kind"] == "hold" or v["rule"] in ("health", "capacity") for v in res.violations)
            why = "; ".join(f"{v['rule']}: {v['detail']}" for v in res.violations)
            if transient:
                return self._wait(task, spec, why, request_text, ctx, retry, out_info)
            return Outcome("rejected", spec=spec, reason=why, rows=[_row("rule", {"spec": spec}, {"decision": "reject", "reason": why})], **out_info)
        live = o.repo.current(tid)
        lease = leasebuilder.build_survey_lease(tid, spec, prop, res, topo=o.topo, clock=o.clock, pack=o.pack,
                                                version=live["version"] + 1 if live else 1,
                                                restricted_zones=list(view.restricted_for(spec["priority"])))
        from . import contracts_check
        contracts_check.require_valid(lease)
        try:
            o.repo.commit(lease, claim_keys_for(lease, o.topo.exclusive_zones()), now=o.clock.now())
        except Conflict as exc:
            return self._wait(task, spec, "; ".join(exc.reasons), request_text, ctx, retry, out_info)
        o.scheduler.remove(tid)
        rows = [_row("cloud", {"request_text": request_text, "context": ctx.digest()},
                     {"decision": "propose_lease", "cluster": cid, "robots": lease["assigned_robots"], "zones": lease["allowed_zones"],
                      "spec": spec, "proposed_by": "classical", "survey": {"assign": res.extra["assign"], "skipped": res.extra["skipped"]}}),
                _row("rule", {"proposal": {"cluster": cid, "robots": free}}, {"decision": "approve", "checks": res.checks}, lease["version"]),
                _row("cloud", {"lease_id": lease["lease_id"]}, {"decision": "issue_lease", "lease_id": lease["lease_id"]}, lease["version"])]
        return Outcome("leased", rows=rows, lease=lease, spec=spec, publish=o._lease_publish(lease, spec), **out_info)

    def _wait(self, task, spec, reason, request_text, ctx, retry, out_info) -> Outcome:
        from datetime import timedelta
        from .clock import parse_iso
        o = self.o
        o.scheduler.push(task["task_id"], spec["priority"], parse_iso(spec["requested_at"]) + timedelta(minutes=spec["deadline_minutes"]))
        rows = [] if retry else [_row("cloud", {"request_text": request_text, "context": ctx.digest()}, {"decision": "queue", "reason": reason})]
        return Outcome("queued", rows=rows, spec=spec, reason=reason, **out_info)

    def merge(self, task: dict, preview: str | None) -> tuple[str, int]:
        """Merge the edge's survey report into the cloud's picture; returns the summary line and how many counts were updated."""
        o = self.o
        spec = task["spec"]
        try:
            doc = json.loads(preview or "")
        except ValueError:
            return f"Survey of {len(spec['targets'])} place(s) finished; no readable report came back.", 0
        reports = doc.get("reports") or [doc]
        counts: dict[str, dict] = {}
        for r in reports:
            for place, items in (r.get("counts") or {}).items():
                node = o.topo.anchor_of(place) or place
                if node not in o.topo.nodes:
                    continue                  # a place the map does not know is not merged and not counted
                for item, q in (items or {}).items():
                    counts.setdefault(node, {})
                    counts[node][item] = counts[node].get(item, 0) + q if isinstance(q, int) and not isinstance(q, bool) else q
        n = o.fleet.merge_report(counts, {i["id"] for i in o.pack.items})
        found = {p: c.get(spec["item"], 0) for p, c in counts.items() if spec["item"] in c}
        total = sum(v for v in found.values() if isinstance(v, int))
        where = ", ".join(f"{p}: {v}" for p, v in sorted(found.items())) or "none seen"
        return f"Survey done: {total} x {spec['item'].replace('_', ' ')} ({where}).", n
