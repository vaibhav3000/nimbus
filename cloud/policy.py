"""Policy documents (retrieval for the model's context) and *runtime
restrictions* (zones that are held or forbidden, possibly only for some
priority classes) which the Rule Checker enforces. PRD FR-17.

Restriction kinds, their citing policy, effect (hold = wait until lifted,
forbid = never) and exempt priorities come from the pack. A statement like
"Zone Z is now closed to routine work, urgent ones can still go in"
becomes declare("Z", "<kind>"): ordinary work is held, the exempt class is not.

MS1 retrieval is keyword scoring over a handful of short documents, which fits
in the prompt; the Bedrock Knowledge Base swap keeps the same `retrieve` shape.
"""

from __future__ import annotations

import re
import threading

from .pack import Pack


class PolicyBook:
    def __init__(self, pack: Pack):
        self._docs = pack.policy_docs
        self._kinds = pack.restrictions
        self._active: dict[str, dict] = {}  # zone -> {"kind","source"}
        self._lock = threading.Lock()

    def retrieve(self, text: str, *, extra_terms: tuple[str, ...] = (), limit: int = 3) -> list[dict]:
        """Top documents by keyword overlap with the request. Always cites id."""
        words = set(re.findall(r"[a-z0-9\-]+", text.lower())) | {t.lower() for t in extra_terms}
        cited = {self._kinds[a["kind"]]["policy_id"] for a in self._active.values()}
        scored = []
        for p in self._docs:
            score = sum(1 for k in p["keywords"] if k.lower() in words) + (2 if p["id"] in cited else 0)
            if score:
                scored.append((score, p))
        scored.sort(key=lambda sp: (-sp[0], sp[1]["id"]))
        return [dict(p) for _, p in scored[:limit]]

    def get(self, policy_id: str) -> dict:
        return next(p for p in self._docs if p["id"] == policy_id)

    # -- runtime restrictions ----------------------------------------------------------

    def declare(self, zone: str, kind: str, source: str = "policy-owner") -> None:
        if kind not in self._kinds:
            raise KeyError(f"unknown restriction kind {kind!r}; this pack defines {sorted(self._kinds)}")
        with self._lock:
            self._active[zone] = {"kind": kind, "source": source}

    def lift(self, zone: str) -> None:
        with self._lock:
            self._active.pop(zone, None)

    def restrictions(self) -> dict[str, dict]:
        """zone -> {kind, policy, effect, exempt_priorities, source}."""
        with self._lock:
            return {z: {"kind": a["kind"], "policy": self._kinds[a["kind"]]["policy_id"],
                        "effect": self._kinds[a["kind"]]["effect"],
                        "exempt_priorities": list(self._kinds[a["kind"]].get("exempt_priorities", [])),
                        "source": a["source"]} for z, a in self._active.items()}
