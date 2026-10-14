"""Fleet registry: which edge clusters exist, their robots and stock, and
whether each is *alive*. This is the "quick health check before delegating".

Health is passive first: every check-in (PRD: regular edge heartbeat) updates
`last_seen` and optionally a robot/stock digest. Active probing is only for a
cluster that *was* alive and went quiet (stale); a cluster never heard from
is `unknown` (dev/demo with no edge running), which allocation may permit.
"""

from __future__ import annotations

import threading
from datetime import datetime

from .clock import Clock, iso
from .pack import Pack

HEALTHY_S = 30.0   # check-ins are every few seconds (PRD); 30 s of silence = stale
DOWN_S = 120.0     # beyond this the cluster is treated as down


class FleetRegistry:
    def __init__(self, seed: dict, clock: Clock, *, healthy_s: float = HEALTHY_S, down_s: float = DOWN_S):
        self.clock = clock
        self.healthy_s = healthy_s
        self.down_s = down_s
        self._lock = threading.RLock()
        self.clusters: dict[str, dict] = {}
        for cid, c in seed["clusters"].items():
            self.clusters[cid] = {
                "area": c["area"],
                "home": c["home"],
                "robots": {r: {"state": "idle", "zone": c["home"], "battery": 1.0} for r in c["robots"]},
                "stock": dict(c["stock"]),
                "last_seen": None,  # datetime | None
            }

    @classmethod
    def load(cls, clock: Clock, pack: Pack, **kw) -> "FleetRegistry":
        return cls(pack.fleet_doc, clock, **kw)

    # -- health -------------------------------------------------------------------

    def heartbeat(self, cluster_id: str, digest: dict | None = None) -> bool:
        """Record a check-in. Unknown clusters are ignored (P4: the stand-in
        never rejected a check-in for them) and reported False."""
        with self._lock:
            c = self.clusters.get(cluster_id)
            if c is None:
                return False
            c["last_seen"] = self.clock.now()
            digest = digest or {}
            for r in digest.get("robots", []):
                if r.get("id") in c["robots"]:
                    c["robots"][r["id"]].update({k: r[k] for k in ("state", "zone", "battery") if k in r})
            for item, qty in (digest.get("stock") or {}).items():
                if isinstance(qty, int) and qty >= 0:
                    c["stock"][item] = qty
            return True

    def health(self, cluster_id: str) -> str:
        with self._lock:
            seen: datetime | None = self.clusters[cluster_id]["last_seen"]
            if seen is None:
                return "unknown"
            age = (self.clock.now() - seen).total_seconds()
            return "healthy" if age <= self.healthy_s else "stale" if age <= self.down_s else "down"

    # -- stock ---------------------------------------------------------------------

    def reserve_stock(self, cluster_id: str, item: str) -> bool:
        with self._lock:
            stock = self.clusters[cluster_id]["stock"]
            if stock.get(item, 0) < 1:
                return False
            stock[item] -= 1
            return True

    def release_stock(self, cluster_id: str, item: str) -> None:
        with self._lock:
            stock = self.clusters[cluster_id]["stock"]
            stock[item] = stock.get(item, 0) + 1

    # -- views -----------------------------------------------------------------------

    def snapshot(self) -> list[dict]:
        """What the model (and GET /fleet) sees."""
        with self._lock:
            out = []
            now = self.clock.now()
            for cid in sorted(self.clusters):
                c = self.clusters[cid]
                seen = c["last_seen"]
                out.append({
                    "cluster_id": cid,
                    "area": c["area"],
                    "health": self.health(cid),
                    "last_seen": iso(seen) if seen else None,
                    "age_s": round((now - seen).total_seconds(), 1) if seen else None,
                    "robots": [{"id": r, **v} for r, v in sorted(c["robots"].items())],
                    "stock": dict(sorted(c["stock"].items())),
                })
            return out
