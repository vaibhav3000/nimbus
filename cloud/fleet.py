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
    def __init__(self, seed: dict, clock: Clock, *, healthy_s: float = HEALTHY_S, down_s: float = DOWN_S,
                 zones: set | None = None, stock_store=None):
        self.clock = clock
        self.zones = zones              # map node ids; a reported zone outside them is ignored (None: accept any)
        self.stock_store = stock_store  # durable stock (DynamoDB); None keeps counts in memory only
        self.healthy_s = healthy_s
        self.down_s = down_s
        self._lock = threading.RLock()
        self._held: dict[tuple, int] = {}   # (cluster, item) -> leased but not yet delivered
        self.clusters: dict[str, dict] = {}
        for cid, c in seed["clusters"].items():
            self.clusters[cid] = {
                "area": c["area"],
                "home": c["home"],
                "robots": {r: {"state": "idle", "zone": c["home"], "battery": 1.0} for r in c["robots"]},
                "stock": dict(c["stock"]),
                "last_seen": None,  # datetime | None
            }
        # Equipment that sits in a place other than a store (e.g. spare units kept in a room). Durable rows use the
        # pseudo-cluster id "@<place>" so the stock table needs no new shape.
        self.places: dict[str, dict] = {p: dict(items) for p, items in seed.get("places", {}).items()}
        if self.stock_store is not None:
            self.stock_store.seed({**self.clusters, **{"@" + p: {"stock": i} for p, i in self.places.items()}})   # rows that are missing only: a restart keeps real counts
            self.sync_stock()

    @classmethod
    def load(cls, clock: Clock, pack: Pack, **kw) -> "FleetRegistry":
        kw.setdefault("zones", set(pack.topology_doc["nodes"]))
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
                    known = {k: r[k] for k in ("state", "zone", "battery", "pos") if k in r}
                    if "pos" in known and not (isinstance(known["pos"], list) and len(known["pos"]) == 2
                                               and all(isinstance(v, (int, float)) for v in known["pos"])):
                        known.pop("pos")      # optional metres in the map frame; used only to draw the robot smoothly
                    if self.zones is not None and known.get("zone") not in self.zones:
                        known.pop("zone", None)   # not a map node: keep the last zone we could route from
                    c["robots"][r["id"]].update(known)
            # The edge counts what is on its shelves; items already leased out are still there until
            # delivered, so its digest must not hand them back to the allocator.
            self._write_stock(cluster_id, {i: max(0, q - self._held.get((cluster_id, i), 0))
                                           for i, q in (digest.get("stock") or {}).items()
                                           if isinstance(q, int) and not isinstance(q, bool)})
            return True

    def health(self, cluster_id: str) -> str:
        with self._lock:
            seen: datetime | None = self.clusters[cluster_id]["last_seen"]
            if seen is None:
                return "unknown"
            age = (self.clock.now() - seen).total_seconds()
            return "healthy" if age <= self.healthy_s else "stale" if age <= self.down_s else "down"

    # -- stock ---------------------------------------------------------------------

    def _write_stock(self, cluster_id: str, counts: dict) -> dict:
        """Absolute counts (a check-in digest or an operator restock): memory, and the durable rows."""
        stock = self.clusters[cluster_id]["stock"]
        for item, qty in counts.items():
            if isinstance(qty, int) and not isinstance(qty, bool) and qty >= 0:
                if self.stock_store is not None and stock.get(item) != qty:
                    self.stock_store.set(cluster_id, item, qty)
                stock[item] = qty
        return dict(stock)

    def set_stock(self, cluster_id: str, counts: dict) -> dict:
        with self._lock:
            return self._write_stock(cluster_id, counts)

    def sync_stock(self) -> None:
        """Re-read the durable counts (after a lost race, or at startup)."""
        if self.stock_store is None:
            return
        with self._lock:
            for cid, items in self.stock_store.load().items():
                if cid in self.clusters:
                    self.clusters[cid]["stock"].update(items)
                elif cid.startswith("@") and cid[1:] in self.places:
                    self.places[cid[1:]].update(items)

    def robot_zone(self, robot_id: str) -> str | None:
        """Where the edge last said this robot is (a map node), or None if it never has."""
        with self._lock:
            for c in self.clusters.values():
                if robot_id in c["robots"]:
                    return c["robots"][robot_id].get("zone")
        return None

    def place_stock(self, place: str) -> dict:
        with self._lock:
            return {i: q for i, q in self.places.get(place, {}).items() if q > 0}

    def move_stock(self, place: str, cluster_id: str, item: str, n: int = 1) -> bool:
        """Equipment carried from a place into a cluster's store. False (and nothing moves) if the place holds fewer than n."""
        with self._lock:
            held = self.places.get(place, {})
            if n < 1 or held.get(item, 0) < n:
                return False
            held[item] -= n
            self.clusters[cluster_id]["stock"][item] = self.clusters[cluster_id]["stock"].get(item, 0) + n
            if self.stock_store is not None:
                self.stock_store.add("@" + place, item, -n)
                self.stock_store.add(cluster_id, item, n)
            return True

    def return_stock(self, cluster_id: str, item: str) -> None:
        """Give an item back for good (a cancelled task): memory and the durable row."""
        with self._lock:
            self.release_stock(cluster_id, item)
            if self.stock_store is not None:
                self.stock_store.add(cluster_id, item, 1)

    def reserve_stock(self, cluster_id: str, item: str) -> bool:
        with self._lock:
            stock = self.clusters[cluster_id]["stock"]
            if stock.get(item, 0) < 1:
                return False
            stock[item] -= 1
            self._held[(cluster_id, item)] = self._held.get((cluster_id, item), 0) + 1
            return True

    def release_stock(self, cluster_id: str, item: str) -> None:
        with self._lock:
            stock = self.clusters[cluster_id]["stock"]
            stock[item] = stock.get(item, 0) + 1
            self._unhold(cluster_id, item)

    def settle_stock(self, cluster_id: str, item: str) -> None:
        """The item was delivered: it stays consumed, and the edge's own count will say so."""
        with self._lock:
            self._unhold(cluster_id, item)

    def _unhold(self, cluster_id: str, item: str) -> None:
        key = (cluster_id, item)
        if self._held.get(key, 0) > 0:
            self._held[key] -= 1

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

    def place_snapshot(self) -> dict:
        with self._lock:
            return {p: dict(sorted(i.items())) for p, i in sorted(self.places.items())}
