"""Rule Checker: the deterministic validator that disposes (PRD FR-15).

It re-derives every fact itself from the registry, the claims and the
topology, never trusting the proposer, then applies the pack's rules through
the safe rule engine. The cloud never issues a lease this checker has not
approved. Returns every violation, not just the first, so the model can be
told exactly what to fix on its one retry.

Violation kinds: "veto" (never), "hold" (wait until a restriction lifts),
"clarify" (ask the requester, then it may pass).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import rules
from .allocator import FleetView, needs_stock


@dataclass
class Proposal:
    cluster_id: str
    robot_ids: list
    rationale: str = ""
    source: str = "llm"  # llm | classical


@dataclass
class CheckResult:
    ok: bool
    checks: list = field(default_factory=list)       # names of checks that passed
    violations: list = field(default_factory=list)   # {"rule","detail","kind", ...}
    routes: list = field(default_factory=list)       # [[zone,...], ...] primary first
    source_zone: str | None = None                   # where the robot picks the item up
    anchor: str | None = None                        # routable node of the destination
    approach: list = field(default_factory=list)     # robot's current zone -> pickup, when it is not already there
    extra: dict = field(default_factory=dict)        # task-type specific results (a survey's assignment and skipped places)


def check(spec: dict, proposal: Proposal, view: FleetView, *, holds_stock: bool = False) -> CheckResult:
    topo, fleet, pack = view.topo, view.fleet, view.pack
    res = CheckResult(ok=False)
    bad = res.violations

    def violate(rule: str, detail: str, kind: str = "veto", **extra) -> None:
        bad.append({"rule": rule, "detail": detail, "kind": kind, **extra})

    cluster = fleet.clusters.get(proposal.cluster_id)
    if cluster is None:
        violate("cluster", f"unknown cluster {proposal.cluster_id!r}")
        return res
    res.checks.append("cluster")

    if view.health_ok(proposal.cluster_id):
        res.checks.append("health")
    else:
        violate("health", f"{proposal.cluster_id} health is {fleet.health(proposal.cluster_id)}")

    unknown = [r for r in proposal.robot_ids if r not in cluster["robots"]]
    if unknown:
        violate("robots", f"robots {unknown} are not in {proposal.cluster_id}")
    else:
        res.checks.append("robots")
        busy = [r for r in proposal.robot_ids if not view.robot_free(r)]
        failed = [r for r in proposal.robot_ids if cluster["robots"][r]["state"] == "failed"]
        if busy or failed:
            violate("capacity", f"robots not available: busy={busy} failed={failed}")
        else:
            res.checks.append("capacity")

    if holds_stock or not needs_stock(spec, pack) or cluster["stock"].get(spec["item"], 0) >= 1:
        res.checks.append("stock")
    else:
        violate("stock", f"{proposal.cluster_id} has no {spec['item']}")

    dest = topo.anchor_of(spec["destination"])
    src_zone = spec.get("source")
    src = topo.anchor_of(src_zone) if src_zone else None
    res.anchor = dest
    if proposal.cluster_id in view.excluded_clusters:
        violate("cluster", f"{proposal.cluster_id} is excluded for this decision")
        return res
    # a hand-over leg ends at a hand-off zone, which need not be a place an item is normally delivered to
    if dest is None or not (spec.get("leg") or topo.is_destination(spec["destination"])):
        violate("destination", f"unknown destination {spec['destination']!r}")
    elif src_zone and src is None:
        violate("destination", f"unknown source {src_zone!r}")
    else:
        res.checks.append("destination")
        zones = topo.cluster_zones(proposal.cluster_id)
        restricted = view.restricted_for(spec["priority"])
        bound = next((z for z in (dest, src) if z in restricted), None)
        if dest not in zones or (src is not None and src not in zones):
            violate("reachability", f"{spec['destination']} is outside {proposal.cluster_id}'s zones (hand-over needed)")
        elif bound:
            r = restricted[bound]
            violate("policy", f"{bound} is {r['effect']} by {r['reason']}",
                    kind="hold" if r["effect"] == "hold" else "veto", policy=r["policy"])
        else:
            start = src if src is not None else topo.home_of(proposal.cluster_id)
            routes = topo.routes(start, dest, allowed=zones - set(restricted), avoid=set(restricted))
            approach: list = []
            if routes and not unknown:
                here = cluster["robots"][proposal.robot_ids[0]]["zone"]
                if here != start:   # the robot must be able to get from where it is to the start inside the lease
                    # a robot standing in a zone that has just been shut may leave it, so `here` is not avoided
                    shut = set(restricted) - {here}
                    approach = topo.shortest_path(here, start, allowed=(zones - shut) | {here}, avoid=shut) or []
                    if not approach:
                        routes = []
            if routes:
                res.approach = approach
                res.checks.append("reachability")
                res.checks += sorted({f"policy:{r['policy']}" for r in restricted.values() if r["origin"] == "policy"})
                res.routes = routes
                res.source_zone = topo.canonical_zone(src_zone) if src_zone else start
            else:
                violate("reachability", f"no route {start}->{dest} avoiding restricted zones")

    # The pack's rules (confirmation, vetoes) over this proposal's facts.
    facts = {
        "spec": spec,
        "dest": topo.facts_for(spec["destination"]),
        "source": topo.facts_for(src_zone),
        "priority": spec["priority"],
        "cluster": {"id": proposal.cluster_id, "area": cluster["area"], "health": fleet.health(proposal.cluster_id)},
    }
    out = rules.apply_rules(pack.rules, facts)
    res.checks += out.passed
    bad += out.violations

    res.ok = not bad
    return res


def check_survey(spec: dict, proposal: Proposal, view: FleetView) -> CheckResult:
    """The Rule Checker for a survey (a look-around job): the cluster is healthy, its robots are free, and each place to
    visit is inside the cluster, not restricted for this priority, and reachable. Places that fail are SKIPPED and
    reported (one closed place does not cancel the sweep); only if none is left is the survey refused. Assigns the places
    to the robots nearest-first and gives each robot's full path."""
    topo, fleet = view.topo, view.fleet
    res = CheckResult(ok=False)
    bad = res.violations

    def violate(rule: str, detail: str, kind: str = "veto") -> None:
        bad.append({"rule": rule, "detail": detail, "kind": kind})

    cluster = fleet.clusters.get(proposal.cluster_id)
    if cluster is None:
        violate("cluster", f"unknown cluster {proposal.cluster_id!r}")
        return res
    if not view.health_ok(proposal.cluster_id):
        violate("health", f"{proposal.cluster_id} health is {fleet.health(proposal.cluster_id)}")
    unknown = [r for r in proposal.robot_ids if r not in cluster["robots"]]
    if unknown:
        violate("robots", f"robots {unknown} are not in {proposal.cluster_id}")
    else:
        busy = [r for r in proposal.robot_ids if not view.robot_free(r) or cluster["robots"][r]["state"] == "failed"]
        if busy:
            violate("capacity", f"robots not available: {busy}")
    if bad:
        return res
    zones = topo.cluster_zones(proposal.cluster_id)
    restricted = view.restricted_for(spec["priority"])
    avoid = set(restricted)
    skipped, todo = [], []
    for t in spec["targets"]:
        if t not in zones:
            skipped.append({"target": t, "why": f"outside {proposal.cluster_id}'s area"})
        elif t in restricted:
            r = restricted[t]
            skipped.append({"target": t, "why": f"{r['effect']} by {r['reason']}"})
        else:
            todo.append(t)
    pos = {r: cluster["robots"][r]["zone"] if cluster["robots"][r]["zone"] in zones else topo.home_of(proposal.cluster_id)
           for r in proposal.robot_ids}
    assign: dict[str, list] = {r: [] for r in proposal.robot_ids}
    paths: dict[str, list] = {r: [pos[r]] for r in proposal.robot_ids}
    remaining = list(todo)
    travelled = {r: 0.0 for r in proposal.robot_ids}
    while remaining:
        best = None
        for r in proposal.robot_ids:
            for t in remaining:
                p = topo.shortest_path(pos[r], t, allowed=zones - avoid | {pos[r]}, avoid=avoid - {pos[r]})
                # balance the robots: the cheapest way to finish is the shortest total route for whoever takes it
                if p and (best is None or travelled[r] + topo.path_length(p) < best[0]):
                    best = (travelled[r] + topo.path_length(p), r, t, p)
        if best is None:
            skipped += [{"target": t, "why": "no route inside the cluster"} for t in remaining]
            break
        _, r, t, p = best
        assign[r].append(t); paths[r] += p[1:]; pos[r] = t; remaining.remove(t); travelled[r] += topo.path_length(p)
    if not any(assign.values()):
        violate("reachability", "no place can be surveyed: " + "; ".join(f"{s['target']}: {s['why']}" for s in skipped),
                "hold" if any("hold" in s["why"] for s in skipped) else "veto")
        return res
    res.checks += ["cluster", "health", "robots", "capacity", "reachability"]
    res.routes = [paths[r] for r in proposal.robot_ids if assign[r]]
    res.extra = {"assign": {r: ts for r, ts in assign.items() if ts}, "paths": {r: p for r, p in paths.items() if assign[r]}, "skipped": skipped}
    res.ok = True
    return res
