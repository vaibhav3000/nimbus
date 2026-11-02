"""Statements: a message that tells the system a rule about a place instead of asking for a delivery (SC-3).

Applying one is deterministic and idempotent: declare (or lift) the restriction, hold the work it now blocks
(stopping robots already on the way), create the tasks the message asked for (moving equipment out), and tell the
people and clusters it affects. The model's part is only recognising the statement and naming the place, the kind
and the items (the `announce_restriction` tool); everything here is checked against the pack and the map.
"""

from __future__ import annotations

from .clock import iso
from .notify import Notice
from .orchestrator_types import Outcome, row as _row

MAX_CHILDREN = 8


class Statements:
    def __init__(self, orch):
        self.o = orch

    def apply(self, task: dict, st: dict, text: str) -> Outcome:
        o = self.o
        zone, kind = st["zone"], st["kind"]
        pol = o.pack.restrictions[kind]
        active = o.policies.restrictions().get(zone)
        rows, notices, children, others = [], [], [], []

        def say(n: Notice) -> None:
            entry, pub = o.notifier.send(n)
            rows.append(_row("cloud", {"task_id": task["task_id"]},
                             {"decision": "notify", "audience": n.audience, "type": n.type, "text": n.text,
                              "cluster_id": n.cluster_id, "zone": n.zone, "message_id": entry["message_id"]}))
            if pub:
                publish.append(pub)

        publish: list = []
        if st.get("lift"):
            if active is None:
                return self._done(task, rows, "nothing to lift: that place has no restriction", [], [], [], [], False)
            o.policies.lift(zone)
            rows.append(_row("cloud", {"text": text}, {"decision": "lift_restriction", "zone": zone, "kind": kind,
                                                       "policy": pol["policy_id"]}))
            say(Notice("place", f"The {kind} restriction on {zone} is lifted; held work resumes.", type="MAP", zone=zone))
            say(Notice("operator", f"{kind} lifted on {zone}.", task_id=task["task_id"]))
            for c in o.mapstate.affected_clusters(zone, o.repo, o.clock.now()):
                say(Notice("cluster", f"{zone}: {kind} lifted.", type="MAP", cluster_id=c))
            return self._done(task, rows, f"{kind} lifted on {zone}; held work is being released.", [], [], publish, [], True)

        if active is not None and active["kind"] == kind:   # saying it twice changes nothing
            return self._done(task, rows, f"{zone} is already under {kind}; nothing changed.", [], [], [], [], False)

        o.policies.declare(zone, kind, source=f"statement:{task['task_id']}")
        rows.append(_row("cloud", {"text": text}, {"decision": "declare_restriction", "zone": zone, "kind": kind,
                                                   "effect": pol["effect"], "policy": pol["policy_id"],
                                                   "exempt_priorities": pol.get("exempt_priorities", [])}))
        held = []
        exempt = set(pol.get("exempt_priorities", []))
        for lease in o.repo.live_leases():
            other = o.load_task(_task_id(lease))
            if other is None or other["task_id"] == task["task_id"] or not other.get("spec"):
                continue
            spec = o.current_spec(other)
            if spec["priority"] in exempt:
                continue
            ends = {o.topo.anchor_of(spec["destination"]), o.topo.anchor_of(spec["source"]) if spec.get("source") else None}
            if zone in ends:
                out = o.hold(other, f"{pol['policy_id']}: {zone} is under {kind}")
                if out.kind == "held":
                    others.append((other, out))
                    held.append(other["task_id"])

        items = [i for i in st.get("extract_items", []) if i in {x["id"] for x in o.pack.items}]
        area = o.topo.nodes[zone]["area"]
        store = next((o.topo.home_of(c) for c, a in sorted(o.topo.cluster_area.items()) if a == area), None) \
            or o.topo.home_of(sorted(o.topo.cluster_area)[0])
        first_class = min(o.pack.priorities, key=lambda p: p["rank"])
        priority = (sorted(exempt, key=lambda p: o.pack.priority(p)["rank"]) or [first_class["id"]])[0]
        for item in items:
            for _ in range(o.fleet.place_stock(zone).get(item, 0)):
                if len(children) < MAX_CHILDREN:
                    children.append({"item": item, "source": zone, "destination": store, "priority": priority,
                                     "deadline_minutes": o.pack.priority(priority)["default_deadline_minutes"],
                                     "constraints": [], "rationale": f"move {item} out of {zone} ({kind})"})

        say(Notice("place", f"{zone} is now under {kind}. {pol['effect'].capitalize()} for ordinary work; "
                            f"{', '.join(sorted(exempt)) or 'nothing'} may still enter."
                            + (f" {len(children)} item(s) are being moved out by robot." if children else ""),
                   type="WARNING", zone=zone))
        say(Notice("operator", f"{kind} declared on {zone} by {task['task_id']}: {len(held)} task(s) held, "
                               f"{len(children)} extraction task(s) created.", task_id=task["task_id"]))
        for c in o.mapstate.affected_clusters(zone, o.repo, o.clock.now()):
            say(Notice("cluster", f"{zone} is under {kind}: ordinary work there is held.", type="MAP", cluster_id=c))
        summary = (f"{zone} is now under {kind}. {len(held)} task(s) in progress were held, "
                   f"{len(children)} task(s) will move equipment out, and the people there and the operator were told.")
        return self._done(task, rows, summary, children, others, publish, [], False)

    def _done(self, task, rows, summary, children, others, publish, _unused, drain) -> Outcome:
        return Outcome("statement", rows=rows, reason=summary, notice=summary, publish=publish, children=children,
                       others=others, drain=drain)


def _task_id(lease: dict) -> str:
    from .leases import _task_of
    return _task_of(lease)
