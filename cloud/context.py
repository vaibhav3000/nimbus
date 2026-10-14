"""The context pack: everything the cloud LLM is allowed to know when it turns
a request into a task spec and a proposed allocation.

Layers (docs/p3/06): the pack's instructions (static); the situation (fleet
health, free robots, stock, active restrictions); the task (the conversation
as messages plus a *task log* of what has happened since: leases issued,
edge events, re-plans, queue decisions); and on-demand lookups (read tools).
`digest()` is the compact, replayable summary that goes into the decision
log; `to_prompt()` is what the model reads.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .pack import Pack
from .topology import Topology

# Decision-log events that are conversation or bookkeeping, not consequences.
_SKIP = {"request_received", "clarification_answered", "ask_clarify", "propose_lease", "approve"}
_DETAIL_KEYS = ("cluster", "robots", "reason", "why", "what_happened", "where", "result_ref", "blocked_zones", "lease_id")


def summarise_log(rows: list[dict], limit: int = 12) -> list[str]:
    """Compact 'what happened so far' lines from decision-log rows (oldest
    first). These are the *consequences* of earlier actions: the model sees
    the world as it now is, not just what was said."""
    lines = []
    for r in rows:
        out = r.get("output", {})
        ev = out.get("decision") or out.get("event") or "?"
        if r.get("who") == "requester" or ev in _SKIP:
            continue
        bits = {}
        for src in (out, r.get("inputs", {})):
            for k in _DETAIL_KEYS:
                if k in src and k not in bits and src[k] not in (None, "", [], {}):
                    bits[k] = src[k]
        detail = " ".join(f"{k}={json.dumps(v) if not isinstance(v, str) else v}" for k, v in bits.items())
        ver = f" v{r['lease_version']}" if r.get("lease_version") else ""
        lines.append(f"{r.get('timestamp', '')[11:19]} {r['who']}: {ev}{ver}" + (f" ({detail})" if detail else ""))
    return lines[-limit:]


@dataclass
class Context:
    now: str
    mode: str
    pack_id: str
    topology_version: str
    item_lines: list
    destination_lines: list
    source_lines: list
    fleet: list
    policies: list
    restrictions: dict
    busy_robots: list = field(default_factory=list)
    probes_sent: list = field(default_factory=list)
    task_log: list = field(default_factory=list)

    @classmethod
    def build(cls, pack: Pack, topo: Topology, *, now: str, mode: str, fleet: list, policies: list,
              restrictions: dict, busy_robots: list, probes_sent: list, history: list | None) -> "Context":
        labels = pack.ontology.get("flag_labels", {})
        dest_lines = []
        for node_id, attrs in sorted(topo.destinations().items()):
            flags = "".join(f", {lab}" for flag, lab in labels.items() if attrs.get(flag))
            dest_lines.append(pack.ontology["destination_line"].format_map(
                _Blank({**attrs, "id": node_id, "flags": flags})))
        return cls(now=now, mode=mode, pack_id=pack.id, topology_version=topo.version,
                   item_lines=[f"{i['id']} ({i['label']})" for i in pack.items],
                   destination_lines=dest_lines,
                   source_lines=[f"{n} (area {d['area']})" for n, d in sorted(topo.sources().items())], fleet=fleet, policies=policies, restrictions=restrictions,
                   busy_robots=busy_robots, probes_sent=probes_sent, task_log=summarise_log(history or []))

    def digest(self) -> dict:
        return {
            "pack": self.pack_id,
            "topology": self.topology_version,
            "health": {c["cluster_id"]: c["health"] for c in self.fleet},
            "free_robots": {
                c["cluster_id"]: [r["id"] for r in c["robots"]
                                  if r["state"] != "failed" and r["id"] not in self.busy_robots]
                for c in self.fleet
            },
            "policies": [p["id"] for p in self.policies],
            "restrictions": {z: f"{r['policy']}:{r['effect']}" for z, r in self.restrictions.items()},
            "probes_sent": self.probes_sent,
            "task_log_lines": len(self.task_log),
        }

    def to_prompt(self) -> str:
        lines = [f"CONTEXT (as of {self.now}, mode {self.mode}, pack {self.pack_id}, topology {self.topology_version})"]
        lines.append("Items: " + ", ".join(self.item_lines))
        lines.append("Destinations: " + "; ".join(self.destination_lines))
        lines.append("Depots (where stock is kept; also valid places to return items to): " + "; ".join(self.source_lines))
        lines.append("Fleet:")
        for c in self.fleet:
            robots = ", ".join(
                f"{r['id']}={'busy' if r['id'] in self.busy_robots else r['state']}" for r in c["robots"])
            stock = ", ".join(f"{k}={v}" for k, v in c["stock"].items() if v)
            lines.append(f"  {c['cluster_id']} area={c['area']} health={c['health']} robots[{robots}] stock[{stock}]")
        if self.restrictions:
            lines.append("Restrictions (hold = wait until lifted, forbid = never; exempt classes may still enter):")
            for z, r in sorted(self.restrictions.items()):
                ex = f", exempt: {', '.join(r['exempt_priorities'])}" if r["exempt_priorities"] else ""
                lines.append(f"  {z}: {r['effect']} by {r['policy']}{ex}")
        lines.append("Relevant rules:")
        for p in self.policies:
            lines.append(f"  [{p['id']}] {p['title']}: {p['text']}")
        if self.task_log:
            lines.append("Task log so far (oldest first):")
            lines += [f"  {l}" for l in self.task_log]
        return "\n".join(lines)


class _Blank(dict):
    """format_map helper: a missing attribute renders as empty, not an error."""
    def __missing__(self, key):
        return ""
