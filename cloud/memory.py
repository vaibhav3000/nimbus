"""Long-term memory (PRD FR-19): what has been confirmed about a place, kept so the cloud can stop asking.

It remembers which variant of an item a place keeps getting (the pack says which items are variants of which, so
this file knows no domain). After a choice has been confirmed `CONFIRMATIONS` times for a place, a request that
names only the base item is read as that variant. Memory never overrides a safety rule: the Rule Checker still
runs on the result, so a remembered "standard" for a place whose rule demands the special variant still asks.
"""

from __future__ import annotations

import threading

CONFIRMATIONS = 2


class Memory:
    def __init__(self, durable=None):
        self.durable = durable
        self._lock = threading.Lock()
        self._habits: dict[str, dict] = durable.load("habit") if durable is not None else {}   # "place|base" -> {item: count}

    @staticmethod
    def _key(place: str, base: str) -> str:
        return f"{place}|{base}"

    def record(self, place: str, base_item: str, item: str) -> None:
        """A request for `base_item` at `place` was leased as `item` (a variant, or the base itself)."""
        with self._lock:
            counts = self._habits.setdefault(self._key(place, base_item), {})
            counts[item] = counts.get(item, 0) + 1
            if self.durable is not None:
                self.durable.put("habit", self._key(place, base_item), counts)

    def usual(self, place: str, base_item: str) -> str | None:
        """The variant this place has confirmed at least CONFIRMATIONS times (most often, ties to the later-seen one)."""
        with self._lock:
            counts = self._habits.get(self._key(place, base_item), {})
        if not counts:
            return None
        item, n = max(counts.items(), key=lambda kv: kv[1])
        return item if n >= CONFIRMATIONS and item != base_item else None

    def lines_for(self, place: str | None) -> list[str]:
        """Context lines for the model: what this place usually gets."""
        if not place:
            return []
        out = []
        with self._lock:
            for key, counts in self._habits.items():
                p, base = key.split("|", 1)
                item, n = max(counts.items(), key=lambda kv: kv[1])
                if p == place and n >= CONFIRMATIONS and item != base:
                    out.append(f"Usual for {place}: a request for {base} has been confirmed as {item} {n} times.")
        return out
