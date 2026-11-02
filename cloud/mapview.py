"""A read-only picture of the whole system for the map page: the map, what is shut or restricted, where the
robots are, which lease each is running, the places' inventory, tasks and recent notices. Pure function of state."""

from __future__ import annotations

from .leases import _task_of


def _layout(topo) -> dict[str, list[float]]:
    """Positions for nodes without coordinates: west left, shared middle, east right, evenly spaced downwards."""
    columns = {"west": -1.0, "shared": 0.0, "east": 1.0}
    seen: dict[str, int] = {}
    out = {}
    for nid, d in topo.nodes.items():
        col = d["area"] if d["area"] in columns else "shared"
        row = seen[col] = seen.get(col, -1) + 1
        out[nid] = [columns[col] * 8.0, -row * 4.0]
    return out


def _route(topo, lease: dict, start: str | None) -> list[str]:
    """The zones the robot walks through for this lease, in order (shortest paths between its go_to targets)."""
    usable = set(lease["allowed_zones"]) - set(lease["forbidden_zones"])
    here, path = start if start in topo.nodes else None, []
    for step in lease["task_sequence"]:
        if step["cmd"] != "go_to":
            continue
        to = topo.anchor_of(step["params"]["to"]) or step["params"]["to"]
        if to not in topo.nodes:
            continue
        leg = [to] if here is None else (topo.shortest_path(here, to, allowed=usable | {here}) or [here, to])
        path += [z for z in leg if not path or path[-1] != z]
        here = to
    return path


def build(orch, tasks: list[dict]) -> dict:
    topo, now = orch.topo, orch.clock.now()
    fallback = _layout(topo)
    nodes = [{"id": n, "kind": d["kind"], "area": d["area"], "label": d.get("label") or n,
              "exclusive": bool(d.get("exclusive")), "x": (d.get("pos") or fallback[n])[0], "y": (d.get("pos") or fallback[n])[1]}
             for n, d in topo.nodes.items()]
    live = orch.repo.live_leases()
    by_robot = {r: l for l in live for r in l["assigned_robots"]}
    task_of = {_task_of(l): l for l in live}
    robots = []
    for c in orch.fleet.snapshot():
        for r in c["robots"]:
            l = by_robot.get(r["id"])
            robots.append({"id": r["id"], "cluster": c["cluster_id"], "zone": r["zone"], "state": r["state"],
                           "task_id": _task_of(l) if l else None})
    leases = [{"task_id": _task_of(l), "cluster": l["cluster_id"], "robots": l["assigned_robots"], "version": l["version"],
               "priority": l["priority"], "allowed": l["allowed_zones"], "forbidden": l["forbidden_zones"],
               "stop": [s["cmd"] for s in l["task_sequence"]] == ["stop"],
               "route": _route(topo, l, orch.fleet.robot_zone(l["assigned_robots"][0]))} for l in live]
    return {
        "pack": orch.pack.id, "mode": orch.mode, "model": getattr(orch.llm, "model_id", "?"), "now": now.isoformat(),
        "nodes": nodes, "edges": [[a, b, w] for a, b, w in orch.pack.topology_doc["edges"]],
        "closed": orch.mapstate.closed(now), "restrictions": orch.policies.restrictions(),
        "clusters": [{"id": c["cluster_id"], "health": c["health"], "stock": c["stock"]} for c in orch.fleet.snapshot()],
        "places": orch.fleet.place_snapshot(), "robots": robots, "leases": leases,
        "tasks": [{"task_id": t["task_id"], "status": t["status"], "text": t["request_text"], "reason": t.get("reason"),
                   "spec": t.get("spec"), "lease_version": (t.get("lease") or {}).get("version"),
                   "cluster": (t.get("lease") or {}).get("cluster_id"), "leg_index": t.get("leg_index"),
                   "legs": len(t["legs"]) if t.get("legs") else 0, "parent": t.get("parent"),
                   "stopped": bool(t.get("lease")) and [s["cmd"] for s in t["lease"]["task_sequence"]] == ["stop"],
                   "live": t["task_id"] in task_of} for t in tasks],
        "notices": orch.notifier.for_audience()[-25:],
    }
