"""Look-ups the model may call during an agent run (tool class "read").

Every function is a pure read of current state: calling them cannot change a
lease, a claim, stock or the map. Results are capped so one call cannot flood
the next prompt.
"""

from __future__ import annotations

import json
from typing import Callable

CAP_CHARS = 2000


class ReadTools:
    def __init__(self, *, topo, fleet, policies, mapstate, clock, task_log: Callable[[str], list] = lambda tid: []):
        self.topo, self.fleet, self.policies, self.mapstate, self.clock = topo, fleet, policies, mapstate, clock
        self.task_log = task_log

    def run(self, name: str, args: dict) -> dict:
        fn = getattr(self, "_" + name, None)
        if fn is None:
            return {"error": f"unknown look-up {name!r}"}
        out = fn(**args)
        if len(json.dumps(out)) > CAP_CHARS:
            out = {"truncated": True, "text": json.dumps(out)[:CAP_CHARS]}
        return out

    def _get_fleet(self) -> dict:
        return {"clusters": [{"cluster_id": c["cluster_id"], "health": c["health"],
                              "robots": [{"id": r["id"], "state": r["state"], "zone": r["zone"]} for r in c["robots"]],
                              "stock": c["stock"]} for c in self.fleet.snapshot()]}

    def _get_stock(self, cluster_id: str | None = None) -> dict:
        snap = {c["cluster_id"]: c["stock"] for c in self.fleet.snapshot()}
        if cluster_id is not None:
            return {cluster_id: snap[cluster_id]} if cluster_id in snap else {"error": f"unknown cluster {cluster_id!r}"}
        return {"stock": snap, "places": self.fleet.place_snapshot()}

    def _get_route(self, src: str, dst: str, avoid: list | None = None) -> dict:
        a, b = self.topo.resolve_node(src), self.topo.resolve_node(dst)
        if a is None or b is None:
            return {"error": f"unknown zone {src if a is None else dst!r}"}
        skip = {self.topo.resolve_node(z) or z for z in (avoid or [])}
        path = self.topo.shortest_path(a, b, avoid=skip)
        return {"path": path, "length": round(self.topo.path_length(path), 2)} if path else {"path": None}

    def _get_policies(self, query: str) -> dict:
        return {"rules": [{"id": p["id"], "title": p["title"], "text": p["text"]} for p in self.policies.retrieve(query)]}

    def _get_task_log(self, task_id: str) -> dict:
        return {"task_id": task_id, "lines": self.task_log(task_id)[-12:]}

    def _get_map(self) -> dict:
        return {"closed": self.mapstate.closed(self.clock.now()),
                "restrictions": {z: f"{r['policy']}:{r['effect']}" for z, r in self.policies.restrictions().items()}}
