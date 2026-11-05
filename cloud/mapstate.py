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
    def __init__(self, topo: Topology, durable=None):
        self.topo, self.durable = topo, durable
        self._lock = threading.Lock()
        self._closed: dict[str, dict] = {}   # zone -> {"state","source","until"}
        self.version = 0
        if durable is not None:        # closures reported before a restart are still shut after it (until their time is up)
            for zone, c in durable.load("closure").items():
                if zone in topo.nodes:
                    self._closed[zone] = c

    def report(self, zone: str, state: str, source: str, now: datetime, ttl_s: int | None = 900) -> str | None:
        """Record a closure. Returns the map node it applies to, or None when `zone` is not on the map."""
        node = self.topo.resolve_node(zone)
        if node is None or state not in CLOSING_STATES:
            return None
        until = iso(now + timedelta(seconds=ttl_s)) if ttl_s else None
        with self._lock:
            self._closed[node] = {"state": state, "source": source, "until": until}
            self.version += 1
            if self.durable is not None:
                self.durable.put("closure", node, self._closed[node], ttl_s=ttl_s)
        return node

    def clear(self, zone: str) -> None:
        with self._lock:
            node = self.topo.resolve_node(zone) or zone
            self._closed.pop(node, None)
            self.version += 1
            if self.durable is not None:
                self.durable.delete("closure", node)

    def reset(self) -> list[str]:
        with self._lock:
            cleared = list(self._closed.keys())
            self._closed.clear()
            self.version += 1
            if self.durable is not None:
                if hasattr(self.durable, "clear_kind"):
                    self.durable.clear_kind("closure")
                else:
                    for node in cleared:
                        self.durable.delete("closure", node)
            return cleared

    def expire(self, now: datetime) -> list[str]:
        """Drop closures whose time is up; returns the zones that reopened (the ticker tells people)."""
        with self._lock:
            gone = [z for z, c in self._closed.items() if c["until"] and parse_iso(c["until"]) <= now]
            for z in gone:
                del self._closed[z]
                if self.durable is not None:
                    self.durable.delete("closure", z)
            if gone:
                self.version += 1
            return gone

    def closed(self, now: datetime) -> dict[str, dict]:
        self.expire(now)
        with self._lock:
            return {z: dict(c) for z, c in self._closed.items()}

    def affected_clusters(self, zone: str, repo, now: datetime) -> list[str]:
        """Clusters that cover `zone` or hold a live lease routed through it (FR-11)."""
        node = self.topo.resolve_node(zone) or zone
        out = {cid for cid in self.topo.cluster_area if node in self.topo.cluster_zones(cid)}
        out |= {l["cluster_id"] for l in repo.live_leases() if node in l["allowed_zones"]}
        return sorted(out)
