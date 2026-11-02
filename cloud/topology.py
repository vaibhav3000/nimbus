"""Location graph: zones, passages, areas. The map the model and the Rule
Checker share (PRD: the building map is a static input; Person 1's Gazebo
export replaces the pack's topology.json, same shape).

Which node kinds can be destinations or sources, and how free text names a
location, come from the pack (ontology + locators); nothing here knows what
any particular kind of place is.
"""

from __future__ import annotations

import heapq
import re

from .pack import Pack


class Topology:
    def __init__(self, pack: Pack):
        doc = pack.topology_doc
        self.pack = pack
        self.version: str = doc["version"]
        self.nodes: dict[str, dict] = doc["nodes"]
        self.cluster_area: dict[str, str] = doc["clusters"]
        self.destination_kinds = set(pack.ontology["destination_kinds"])
        self.source_kinds = set(pack.ontology["source_kinds"])
        self._locators = [dict(l, _rx=re.compile(l["pattern"], re.IGNORECASE)) for l in pack.doc["locators"]]
        # Optional per-node "aliases": other names a node answers to (case-insensitive).
        self._alias: dict[str, str] = {}
        for node_id, attrs in self.nodes.items():
            for name in attrs.get("aliases", []):
                self._alias[name.lower()] = node_id
        self.adj: dict[str, dict[str, float]] = {n: {} for n in self.nodes}
        for a, b, w in doc["edges"]:
            self.adj[a][b] = w
            self.adj[b][a] = w

    # -- facts ------------------------------------------------------------------

    def destinations(self) -> dict[str, dict]:
        return {n: d for n, d in self.nodes.items() if d["kind"] in self.destination_kinds}

    def exclusive_zones(self) -> set[str]:
        return {n for n, d in self.nodes.items() if d.get("exclusive")}

    def resolve_node(self, name: str) -> str | None:
        """A node id, or the node an alias names; None when it is neither."""
        if name in self.nodes:
            return name
        return self._alias.get(name.lower())

    def source_of(self, area: str) -> str:
        """Where stock is kept for an area: the home of a cluster that covers it,
        else a source node placed in that area."""
        for cid, a in sorted(self.cluster_area.items()):
            if a == area and (home := self._fleet_home(cid)):
                return home
        return next(n for n, d in self.nodes.items() if d["kind"] in self.source_kinds and d["area"] == area)

    def home_of(self, cluster_id: str) -> str:
        """The store a cluster's robots start from and collect items at: the
        fleet's declared home, falling back to a source node in its area."""
        return self._fleet_home(cluster_id) or self.source_of(self.cluster_area[cluster_id])

    def _fleet_home(self, cluster_id: str) -> str | None:
        home = self.pack.fleet_doc["clusters"].get(cluster_id, {}).get("home")
        return home if home in self.nodes else None

    def cluster_zones(self, cluster_id: str) -> set[str]:
        """Zones a cluster's robots may use: its area plus the shared passages."""
        area = self.cluster_area[cluster_id]
        return {n for n, d in self.nodes.items() if d["area"] in (area, "shared")}

    def anchor_of(self, zone: str) -> str | None:
        """The routable node a zone belongs to (a node is its own anchor, then an
        alias, then a locator)."""
        if (node := self.resolve_node(zone)):
            return node
        hit = self.locate(zone)
        return hit["anchor"] if hit else None

    def is_destination(self, zone: str) -> bool:
        """A place an item may be taken to: the pack's destination kinds, and
        the source kinds too (returning an item to where stock is kept)."""
        a = self.anchor_of(zone)
        return bool(a) and self.nodes[a]["kind"] in (self.destination_kinds | self.source_kinds)

    def sources(self) -> dict[str, dict]:
        return {n: d for n, d in self.nodes.items() if d["kind"] in self.source_kinds}

    def facts_for(self, zone: str | None) -> dict:
        """Attributes of a location for rule conditions and prompts: the
        anchor's attributes plus zone, anchor and a human label."""
        if not zone:
            return {}
        anchor = self.anchor_of(zone)
        if anchor is None:
            return {"zone": zone}
        label = self.nodes[anchor].get("label") or self.nodes[anchor].get("noun") or self.nodes[anchor]["kind"]
        if zone not in self.nodes and (hit := self.locate(zone)):
            label = hit["label"]
        return {**self.nodes[anchor], "zone": zone, "anchor": anchor, "label": label}

    def canonical_zone(self, zone: str) -> str:
        """The name a lease should use for a zone: a node id when the text is a
        node or an alias (or a locator that resolves to one), else unchanged."""
        if (node := self.resolve_node(zone)):
            return node
        hit = self.locate(zone)
        return hit["zone"] if hit else zone

    def speed_cap(self, zone: str) -> float | None:
        a = self.anchor_of(zone)
        return self.nodes[a].get("speed_cap") if a else None

    # -- text to zone ---------------------------------------------------------------

    def locate(self, text: str) -> dict | None:
        """Resolve a place named in free text to {zone, anchor, label}.

        Locators come from the pack, in order. A child (e.g. a numbered
        sub-location of a node) whose index is out of range resolves to None,
        so the caller asks instead of guessing. Unknown places also resolve to None.
        """
        for loc in self._locators:
            for m in loc["_rx"].finditer(text):
                vals = {}
                for i, g in enumerate(m.groups(), start=1):
                    vals[f"g{i}"], vals[f"G{i}"] = g, g.upper()
                    if g.isdigit():
                        vals[f"n{i}"] = int(g)
                if loc["type"] == "child":
                    parent = loc["parent"].format(**vals)
                    parent = self.resolve_node(parent) or parent
                    vals["parent"] = parent
                    pn = self.nodes.get(parent)
                    if not pn or pn["kind"] not in loc["parent_kinds"]:
                        continue
                    if 1 <= vals[loc["index"].strip("{}")] <= pn.get(loc["limit_attr"], 0):
                        return {"zone": loc["id"].format(**vals), "anchor": parent, "label": loc["label"]}
                    return None
                node = loc["id"].format(**vals)
                node = self.resolve_node(node) or node
                n = self.nodes.get(node)
                if n and (not loc.get("kinds") or n["kind"] in loc["kinds"]):
                    return {"zone": node, "anchor": node, "label": loc["label"]}
        return None

    # -- routing ----------------------------------------------------------------------

    def shortest_path(self, src: str, dst: str, *, allowed: set[str] | None = None,
                      avoid: set[str] = frozenset()) -> list[str] | None:
        """Cheapest route src->dst over allowed zones, never entering `avoid`.

        Dijkstra on a graph of tens of nodes; the PRD calls it A* routing and
        on a graph this small the heuristic buys nothing.
        """
        if src in avoid or dst in avoid:
            return None
        if allowed is not None and (src not in allowed or dst not in allowed):
            return None
        best = {src: 0.0}
        prev: dict[str, str] = {}
        heap = [(0.0, src)]
        while heap:
            cost, node = heapq.heappop(heap)
            if node == dst:
                path = [dst]
                while path[-1] != src:
                    path.append(prev[path[-1]])
                return path[::-1]
            if cost > best.get(node, float("inf")):
                continue
            for nxt, w in self.adj[node].items():
                if nxt in avoid or (allowed is not None and nxt not in allowed):
                    continue
                c = cost + w
                if c < best.get(nxt, float("inf")):
                    best[nxt] = c
                    prev[nxt] = node
                    heapq.heappush(heap, (c, nxt))
        return None

    def routes(self, src: str, dst: str, *, allowed: set[str] | None = None,
               avoid: set[str] = frozenset()) -> list[list[str]]:
        """Primary route plus a detour around each single interior zone of it.

        The lease must already contain the detours' zones: the edge may only
        re-route inside the lease (SC-1: a blocked corridor, no cloud call).
        """
        first = self.shortest_path(src, dst, allowed=allowed, avoid=avoid)
        if first is None:
            return []
        out = [first]
        for blocked in first[1:-1]:
            alt = self.shortest_path(src, dst, allowed=allowed, avoid=set(avoid) | {blocked})
            if alt and alt not in out:
                out.append(alt)
        return out

    def path_length(self, path: list[str]) -> float:
        return sum(self.adj[a][b] for a, b in zip(path, path[1:]))
