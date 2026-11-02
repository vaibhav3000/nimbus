"""Hand-over between clusters (SC-2, FR-16, T5).

A task whose source and destination lie in different clusters' areas is split into
legs. A leg is an ordinary delivery whose destination (leg 1) or source (leg 2) is a
hand-off zone that both clusters may use, so the Rule Checker, lease builder and
commit work on a leg unchanged. Each leg is a new version of the same task.
"""

from __future__ import annotations

from dataclasses import dataclass

from .allocator import FleetView, needs_stock


@dataclass
class HandOver:
    handoff: str
    cluster_a: str   # carries the item to the hand-off
    cluster_b: str   # takes it from there to the destination
    distance: float


def handover_options(spec: dict, view: FleetView) -> list[HandOver]:
    """Hand-off zones that let one cluster carry the item to a zone another cluster can finish from, cheapest first."""
    topo, fleet = view.topo, view.fleet
    dest = topo.anchor_of(spec["destination"])
    src_name = spec.get("source")
    restricted = view.restricted_for(spec["priority"])
    avoid = set(restricted)
    if dest is None or dest in avoid:
        return []
    out: list[HandOver] = []
    for a, ca in sorted(fleet.clusters.items()):
        if a in view.excluded_clusters or not view.health_ok(a):
            continue
        if needs_stock(spec, view.pack) and ca["stock"].get(spec["item"], 0) < 1:
            continue
        zones_a = topo.cluster_zones(a)
        src = topo.anchor_of(src_name) if src_name else topo.home_of(a)
        if src is None or src not in zones_a or src in avoid:
            continue
        for b, cb in sorted(fleet.clusters.items()):
            if b == a or b in view.excluded_clusters or not view.health_ok(b):
                continue
            zones_b = topo.cluster_zones(b)
            if dest not in zones_b or not any(v["state"] != "failed" for v in cb["robots"].values()):
                continue
            for h in sorted((zones_a & zones_b) - avoid - {src, dest}):
                r1 = topo.shortest_path(src, h, allowed=zones_a - avoid, avoid=avoid)
                r2 = topo.shortest_path(h, dest, allowed=zones_b - avoid, avoid=avoid)
                if r1 and r2:
                    out.append(HandOver(h, a, b, topo.path_length(r1) + topo.path_length(r2)))
    out.sort(key=lambda o: (o.distance, o.cluster_a, o.cluster_b, o.handoff))
    return out


def make_legs(spec: dict, opt: HandOver) -> list[dict]:
    """Leg 1 delivers to the hand-off zone; leg 2 picks up there and delivers to the real destination."""
    base = {k: v for k, v in spec.items() if k != "destination_anchor"}
    return [{**base, "destination": opt.handoff, "destination_anchor": opt.handoff, "leg": 1, "legs": 2,
             "final_destination": spec["destination"]},
            {**base, "source": opt.handoff, "destination_anchor": spec.get("destination_anchor"), "leg": 2, "legs": 2}]
