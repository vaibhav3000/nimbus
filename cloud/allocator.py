"""Classical allocator: nearest free robot among eligible clusters.

Used three ways: as the pre-filter the LLM's proposal is compared against, as
the fallback when the LLM is down or its proposal is vetoed twice (FR-15/18),
and as the C3 non-LLM baseline. No model anywhere in this file.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .clock import Clock
from .fleet import FleetRegistry
from .leases import LeaseRepo
from .pack import Pack
from .policy import PolicyBook
from .topology import Topology


@dataclass
class FleetView:
    """Everything allocation and checking read, bundled for one decision."""
    topo: Topology
    fleet: FleetRegistry
    repo: LeaseRepo
    policies: PolicyBook
    clock: Clock
    pack: Pack
    allow_unknown: bool = True
    task_id: str | None = None
    extra_forbidden: dict = field(default_factory=dict)  # zone -> reason (e.g. a blocked passage)

    def robot_free(self, robot_id: str) -> bool:
        held = self.repo.holder(f"robot:{robot_id}", self.clock.now())
        return held is None or held["task_id"] == self.task_id

    def busy_robots(self) -> list[str]:
        return [r for c in self.fleet.clusters.values() for r in c["robots"] if not self.robot_free(r)]

    def health_ok(self, cluster_id: str) -> bool:
        h = self.fleet.health(cluster_id)
        return h == "healthy" or (h == "unknown" and self.allow_unknown)

    def restricted_for(self, priority: str) -> dict:
        """zone -> {policy, effect, origin, reason} of the restrictions that bind a
        task of this priority class. Exempt classes are not bound by a
        restriction (a restriction can exempt a priority class)."""
        out = {}
        for zone, r in self.policies.restrictions().items():
            if priority not in r["exempt_priorities"]:
                out[zone] = {"policy": r["policy"], "effect": r["effect"], "origin": "policy",
                             "reason": f"{r['policy']} ({r['kind']})"}
        for zone, why in self.extra_forbidden.items():
            out[zone] = {"policy": why, "effect": "forbid", "origin": "event", "reason": why}
        return out


@dataclass
class Option:
    cluster_id: str
    robot_id: str
    distance: float


@dataclass
class Exclusion:
    cluster_id: str
    reason: str
    transient: bool  # True: waiting may help (busy, stale, held, out of stock)


def needs_stock(spec: dict, pack: Pack) -> bool:
    """An item is drawn from a cluster's stock unless the task names where it
    already is (moving something out of a place consumes nothing)."""
    return pack.uses_stock(spec["item"]) and not spec.get("source")


def eligible_options(spec: dict, view: FleetView) -> tuple[list[Option], list[Exclusion]]:
    topo, fleet, pack = view.topo, view.fleet, view.pack
    dest = topo.anchor_of(spec["destination"])
    src_zone = spec.get("source")
    src = topo.anchor_of(src_zone) if src_zone else None
    restricted = view.restricted_for(spec["priority"])
    avoid = set(restricted)
    options: list[Option] = []
    excluded: list[Exclusion] = []
    for cid, c in sorted(fleet.clusters.items()):
        zones = topo.cluster_zones(cid)
        if dest not in zones or (src is not None and src not in zones):
            excluded.append(Exclusion(cid, f"destination {spec['destination']} is outside {cid}'s area (hand-over needed)", False))
            continue
        bound = next((z for z in (dest, src) if z in restricted), None)
        if bound:
            r = restricted[bound]
            excluded.append(Exclusion(cid, f"{bound} is {r['effect']} by {r['reason']}", r["effect"] == "hold"))
            continue
        if not view.health_ok(cid):
            excluded.append(Exclusion(cid, f"cluster health is {fleet.health(cid)}", True))
            continue
        if needs_stock(spec, pack) and c["stock"].get(spec["item"], 0) < 1:
            excluded.append(Exclusion(cid, f"no {spec['item']} in stock", True))
            continue
        start = src if src is not None else topo.source_of(c["area"])
        route = topo.shortest_path(start, dest, allowed=zones - avoid, avoid=avoid)
        if route is None:
            excluded.append(Exclusion(cid, "no route inside the cluster avoiding restricted zones", False))
            continue
        free = [(r, v) for r, v in sorted(c["robots"].items()) if v["state"] != "failed" and view.robot_free(r)]
        if not free:
            excluded.append(Exclusion(cid, "no free robot", True))
            continue
        for rid, v in free:
            to_start = topo.shortest_path(v["zone"], start, allowed=zones) if v["zone"] in zones else None
            dist = (topo.path_length(to_start) if to_start else 99.0) + topo.path_length(route)
            options.append(Option(cid, rid, dist))
    options.sort(key=lambda o: (o.distance, o.cluster_id, o.robot_id))
    return options, excluded


def best_option(spec: dict, view: FleetView) -> Option | None:
    options, _ = eligible_options(spec, view)
    return options[0] if options else None
