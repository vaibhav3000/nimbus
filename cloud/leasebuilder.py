"""Build a lease document (P4's contract) from a checked allocation, using the
pack's task templates, priority mapping and lease defaults."""

from __future__ import annotations

import re
from datetime import timedelta

from .checker import CheckResult, Proposal
from .clock import Clock, iso, parse_iso
from .pack import Pack
from .topology import Topology


def _fill(obj, vars_: dict):
    """Substitute {name} placeholders in a template. A string that is exactly
    one placeholder takes the variable's own type (a route stays a list)."""
    if isinstance(obj, dict):
        return {k: _fill(v, vars_) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_fill(v, vars_) for v in obj]
    if isinstance(obj, str):
        m = re.fullmatch(r"\{(\w+)\}", obj)
        if m and m.group(1) in vars_:
            return vars_[m.group(1)]
        return re.sub(r"\{(\w+)\}", lambda mm: str(vars_.get(mm.group(1), mm.group(0))), obj)
    return obj


def build_lease(task_id: str, spec: dict, proposal: Proposal, result: CheckResult, *, topo: Topology,
                clock: Clock, pack: Pack, version: int = 1, restricted_zones: list | None = None) -> dict:
    now = clock.now()
    # Measured from when the requester asked, so a task that waited in the queue
    # keeps its real deadline; never less than a minute of runway.
    asked = parse_iso(spec["requested_at"]) if spec.get("requested_at") else now
    deadline = max(asked + timedelta(minutes=spec["deadline_minutes"]), now + timedelta(seconds=60))
    cfg = pack.doc["lease"]
    allowed: list[str] = []
    for route in [*result.routes, result.approach]:
        for z in route:
            if z not in allowed:
                allowed.append(z)
    for z in (spec["destination"], spec.get("source")):
        if z and z not in allowed:
            allowed.append(z)
    caps = [c for z in allowed if (c := topo.speed_cap(z)) is not None]
    robot = proposal.robot_ids[0]
    item = pack.item(spec["item"]) or {}
    template = pack.doc["task_templates"][item.get("template", pack.doc["default_template"])]
    vars_ = {"source": result.source_zone, "destination": spec["destination"], "route": result.routes[0], "robot": robot}
    return {
        "lease_id": f"lease-{task_id}-v{version}",
        "version": version,
        "priority": pack.priority(spec["priority"])["lease_priority"],
        "deadline_ts": iso(deadline),
        "expiry_ts": iso(deadline + timedelta(seconds=cfg["expiry_grace_seconds"])),
        "allowed_zones": allowed,
        "forbidden_zones": sorted(restricted_zones or []),
        "speed_limit": min([cfg["speed_limit"], *caps]),
        "assigned_robots": [robot],
        "task_sequence": [dict(_fill(step, vars_), robot=robot) for step in template],
        "cluster_id": proposal.cluster_id,
        "when_to_escalate": list(cfg["when_to_escalate"]),
    }
