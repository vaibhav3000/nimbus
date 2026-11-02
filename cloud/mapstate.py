"""The cloud's shared map (FR-17): zones the edge reported blocked, closed or
crowded. A closure binds every priority class (an urgent task cannot use a
corridor that is physically shut), unlike a policy restriction that can exempt
one. Closures expire so a forgotten report cannot close the building for good.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta

from .clock import iso, parse_iso
from .topology import Topology

CLOSING_STATES = ("blocked", "closed", "crowded")


class MapState:
    def __init__(self, topo: Topology):
        self.topo = topo
        self._lock = threading.Lock()
        self._closed: dict[str, dict] = {}   # zone -> {"state","source","until"}

    def report(self, zone: str, state: str, source: str, now: datetime, ttl_s: int | None = 900) -> str | None:
        """Record a closure. Returns the map node it applies to, or None when `zone` is not on the map."""
        node = self.topo.resolve_node(zone)
        if node is None or state not in CLOSING_STATES:
            return None
        until = iso(now + timedelta(seconds=ttl_s)) if ttl_s else None
        with self._lock:
            self._closed[node] = {"state": state, "source": source, "until": until}
        return node

    def clear(self, zone: str) -> None:
        with self._lock:
            self._closed.pop(self.topo.resolve_node(zone) or zone, None)

    def closed(self, now: datetime) -> dict[str, dict]:
        with self._lock:
            for z in [z for z, c in self._closed.items() if c["until"] and parse_iso(c["until"]) <= now]:
                del self._closed[z]
            return {z: dict(c) for z, c in self._closed.items()}

    def affected_clusters(self, zone: str, repo, now: datetime) -> list[str]:
        """Clusters that cover `zone` or hold a live lease routed through it (FR-11)."""
        node = self.topo.resolve_node(zone) or zone
        out = {cid for cid in self.topo.cluster_area if node in self.topo.cluster_zones(cid)}
        out |= {l["cluster_id"] for l in repo.live_leases() if node in l["allowed_zones"]}
        return sorted(out)
