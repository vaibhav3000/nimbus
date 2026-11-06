"""Shared scenario for C2 (shared map) and C6 (re-plan latency): a live lease, then an edge reports an incident.

run_incident():
  1. POST /requests (a delivery); the simulated edge holds the lease it receives (checks in, does not finish)
  2. the edge sends UNEXPECTED_EVENT for a zone on the delivery route (blocked corridor or closed door) or for its own
     robot (drive fault, reported with the robot marked failed in the heartbeat)
  3. measures: the cloud's decision time (the POST /escalations round trip, which includes any model call), the
     time until /map.json shows the closure, the time until the edge sees the next lease version, which action the
     cloud chose and whether the new plan avoids the zone; the model time inside the decision comes from the task trace
  4. optionally sends a fresh request to the same place and checks that its lease routes around the closure (FR-17)
  5. cleans up: reopens the zone, cancels the task, marks the robot idle again
"""

from __future__ import annotations

import random
import time
import uuid

import lib

KINDS = ("blocked_with_detour", "blocked_no_detour", "closed_door", "robot_failed")
REQUESTS = [("Deliver a wheelchair to 2B, routine.", "east_waiting_approach"),
            ("Deliver a blanket to 2C, routine.", "west_waiting_approach"),
            ("Bring a monitor to ward 2B.", "east_waiting_approach"),
            ("Bring an IV pole to ward 2C.", "west_waiting_approach")]


def delivery_route(lease: dict) -> list[str]:
    gos = [s for s in lease.get("task_sequence", []) if s.get("cmd") == "go_to"]
    return list(gos[-1]["params"].get("via") or []) if gos else []


def route_zones(lease: dict) -> set[str]:
    zones = set()
    for s in lease.get("task_sequence", []):
        if s.get("cmd") == "go_to":
            zones |= set(s["params"].get("via") or [])
            if isinstance(s["params"].get("to"), str):
                zones.add(s["params"]["to"])
    return zones


def wait_for(pred, timeout: float, step: float = 0.1):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return None


def legs(lease: dict) -> list[list[str]]:
    return [list(s["params"].get("via") or []) for s in lease.get("task_sequence", []) if s.get("cmd") == "go_to" and s["params"].get("via")]


def _path_exists(adj, usable, a, b, avoid) -> bool:
    seen, todo = {a}, [a]
    while todo:
        n = todo.pop()
        if n == b:
            return True
        for m in adj.get(n, ()):
            if m in usable and m != avoid and m not in seen:
                seen.add(m)
                todo.append(m)
    return False


def classify_zones(target: lib.Target, lease: dict) -> dict:
    """For each zone inside a leg of the lease: does a detour inside the cluster's zones exist without it?"""
    s, m, _ = target.api.get("/map.json", timeout=15)
    s2, f, _ = target.api.get("/fleet", timeout=15)
    area = next(c["area"] for c in f["fleet"] if c["cluster_id"] == lease["cluster_id"])
    usable = {n["id"] for n in m["nodes"] if n.get("area") in (area, "shared")}
    adj = {}
    for a, b, _w in m["edges"]:
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
    out = {}
    for leg in legs(lease):
        for z in leg[1:-1]:
            out[z] = _path_exists(adj, usable, leg[0], leg[-1], z)
    return out


def scatter_robots(target: lib.Target, edges: lib.Edges, rng: random.Random, north: bool = False) -> None:
    """Robots are wherever their last task left them: place each idle robot on a random corridor or ward of its own
    wing (never the store), so routes start from varied places."""
    s, m, _ = target.api.get("/map.json", timeout=15)
    s2, f, _ = target.api.get("/fleet", timeout=15)
    for c in f["fleet"]:
        spots = [n["id"] for n in m["nodes"] if n.get("area") in (c["area"], "shared") and n.get("kind") in ("corridor", "ward", "gate")]
        if north:      # far side of the building: the way to the store passes junctions that have an alternative
            spots = [z for z in spots if z in ("north_hall", "lift_lobby")] or spots
        for r in edges.robots.get(c["cluster_id"], []):
            r["zone"] = rng.choice(spots)
    edges.beat()


def run_incident(target: lib.Target, edges: lib.Edges, kind: str, rng: random.Random, *, probe: bool = False,
                 lease_timeout: float = 40.0) -> dict:
    scatter_robots(target, edges, rng, north=kind == "blocked_with_detour")
    text, dest = rng.choice(REQUESTS)
    rec = {"kind": kind, "request": text, "destination": dest}
    s0, m0, _ = target.api.get("/map.json", timeout=15)
    rec["closures_before"] = sorted((m0.get("closed") or {}).keys()) if s0 == 200 else None
    st, out, dt = target.request(text)
    tid = out.get("task_id")
    rec.update(task_id=tid, request_status=out.get("status"), request_s=round(dt, 3))
    if not tid:
        rec["outcome"] = f"request failed http {st}: {out.get('error')}"
        return rec
    edges.tasks.add(tid)
    if not wait_for(lambda: (tid, 1) in edges.seen, lease_timeout):
        rec["outcome"] = f"no lease v1 (task {out.get('status')}: {out.get('reason')})"
        _cleanup(target, edges, tid, None, None)
        return rec
    lease = edges.leases[(tid, 1)]
    route = delivery_route(lease)
    robot = lease["assigned_robots"][0]
    detour = classify_zones(target, lease)
    want = {"blocked_with_detour": True, "blocked_no_detour": False}.get(kind)
    pool = [z for z, ok in detour.items() if want is None or ok == want]
    zone = rng.choice(sorted(pool)) if pool else None
    leg = next((l for l in legs(lease) if zone in l[1:-1]), route)
    robot_zone = leg[0] if leg else None
    rec.update(cluster=lease["cluster_id"], robot=robot, route=route, legs=legs(lease), zone=zone, robot_zone=robot_zone,
               detour_exists=detour.get(zone))
    if kind != "robot_failed" and zone is None:
        rec["outcome"] = "skipped: no zone of that kind on this lease's route"
        _cleanup(target, edges, tid, None, None)
        return rec
    if kind == "robot_failed":
        for c, robots in edges.robots.items():
            for r in robots:
                if r["id"] == robot:
                    r["state"], r["zone"] = "failed", robot_zone or r["zone"]
        edges.beat()
        payload = {"what_happened": f"{robot} stopped: drive motor fault", "where": robot_zone or "unknown",
                   "robots_saw": [f"{robot}: motor controller fault, cannot move"], "robot_zone": robot_zone}
        zone = None
        rec["zone"] = None
    else:
        state = "closed" if kind == "closed_door" else "blocked"
        what = f"{zone} is blocked by a parked bed trolley" if state == "blocked" else f"the door at {zone} is closed and locked"
        payload = {"what_happened": what, "where": zone, "robots_saw": [f"{robot}: {what}"], "robot_zone": robot_zone,
                   "segments": [{"segment_id": zone, "state": state}]}
    t0 = time.time()
    st, reply, post_s = edges.escalate("UNEXPECTED_EVENT", lease, payload)
    rec.update(escalation_http=st, decision_s=round(post_s, 3), replanned=reply.get("replanned"),
               reply_status=reply.get("status"), reply_version=reply.get("lease_version"), reply_error=reply.get("error"))
    if zone:
        def closed():
            s, m, _ = target.api.get("/map.json", timeout=15)
            return time.time() if s == 200 and zone in (m.get("closed") or {}) else None
        t_map = wait_for(closed, 30, 0.2)
        rec["map_reflected_s"] = round(t_map - t0, 3) if t_map else None
    # the reply says whether a new lease was issued; only then is there a new version for the edge to see
    t_v2 = wait_for(lambda: next((edges.seen[k] for k in edges.seen if k[0] == tid and k[1] >= 2), None),
                    30 if reply.get("replanned") else 2, 0.05)
    rec["edge_saw_new_version_s"] = round(t_v2 - t0, 3) if t_v2 else None
    # what the cloud decided, from the decision log and the trace (model calls inside the reaction)
    s, tl, _ = target.api.get(f"/tasks/{tid}/timeline", timeout=20)
    rows = tl.get("timeline", []) if isinstance(tl, dict) else []
    react = [r for r in rows if r.get("output", {}).get("decision") == "react"]
    rec["action"] = react[-1]["output"].get("action") if react else None
    rec["decided_by"] = react[-1]["output"].get("source") if react else None
    ev_at = next((i for i, r in enumerate(rows) if r.get("output", {}).get("event") == "UNEXPECTED_EVENT"), len(rows))
    rec["decisions_after_event"] = [r.get("output", {}).get("decision") for r in rows[ev_at + 1:] if r.get("output", {}).get("decision")]
    s, tr, _ = target.api.get(f"/tasks/{tid}/trace", timeout=20)
    evs = tr.get("events", [])
    first = next((i for i, e in enumerate(evs) if e.get("kind") == "decision" and "UNEXPECTED_EVENT" in str(e.get("summary", ""))), None)
    llm = [e["detail"] for e in (evs[first + 1:] if first is not None else []) if e.get("kind") == "llm"]
    rec["model_calls"] = len(llm)
    rec["model_ms"] = sum((d.get("ms") or 0) for d in llm)
    rec["model_steps"] = [d.get("step") for d in llm]
    s, task, _ = target.api.get(f"/tasks/{tid}", timeout=20)
    new = task.get("lease") or {}
    rec["task_status"] = task.get("status")
    rec["new_version"] = new.get("version")
    rec["new_cluster"] = new.get("cluster_id")
    rec["new_plan_avoids_zone"] = (zone not in route_zones(new)) if (zone and new.get("version", 1) >= 2) else None
    rec["new_robot"] = (new.get("assigned_robots") or [None])[0]
    rec["new_plan_avoids_failed_robot"] = (rec["new_robot"] != robot) if (kind == "robot_failed" and new.get("version", 1) >= 2) else None
    after = rec["decisions_after_event"]
    if rec["new_version"] and rec["new_version"] >= 2:
        rec["outcome"] = ("hand-over to other cluster" if new.get("cluster_id") != lease["cluster_id"] else
                          "another robot took over" if rec["new_robot"] != robot else "rerouted lease")
    elif "wait_and_retry" in after:
        rec["outcome"] = "wait and retry"
    elif "alert_operator" in after or rec["action"] == "escalate":
        rec["outcome"] = "operator alerted (no route left)" if "replan_failed" in after else "operator alerted"
    else:
        rec["outcome"] = rec["action"] or rec["task_status"]
    if probe and zone:
        rec.update(_probe(target, edges, text, zone))
    _cleanup(target, edges, tid, zone, robot)
    return rec


def _probe(target, edges, text, zone) -> dict:
    """A fresh request to the same place while the closure stands: its lease must not route through the zone."""
    st, out, _ = target.request(text)
    tid = out.get("task_id")
    res = {"probe_task": tid, "probe_status": out.get("status")}
    if tid:
        edges.tasks.add(tid)
        if wait_for(lambda: (tid, 1) in edges.seen, 20):
            lease = edges.leases[(tid, 1)]
            res["probe_route"] = sorted(route_zones(lease))
            res["probe_avoids_zone"] = zone not in route_zones(lease) and zone not in lease.get("allowed_zones", [])
        else:
            s, t, _ = target.api.get(f"/tasks/{tid}")
            res["probe_status"], res["probe_reason"] = t.get("status"), t.get("reason")
            res["probe_avoids_zone"] = True if t.get("status") in ("queued", "rejected") else None   # no lease through it
        _cleanup(target, edges, tid, None, None)
    return res


def _cleanup(target, edges, tid, zone, robot):
    if tid:
        target.api.post(f"/tasks/{tid}/control", {"action": "cancel", "message_id": f"exp-cancel-{uuid.uuid4().hex[:10]}"})
    if zone:
        target.api.post("/map/reopen", {"zone": zone})
    if robot:
        for robots in edges.robots.values():
            for r in robots:
                if r["id"] == robot:
                    r["state"] = "idle"
        edges.beat()
    time.sleep(0.3)
