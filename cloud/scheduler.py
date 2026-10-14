"""Priority and deadline scheduler (FR-14): classes ordered by the pack's
ranks (urgent before routine), earliest deadline first inside a class,
arrival order breaks ties.

It decides *order*, never allocation. A queued task is retried whenever
capacity frees up; a blocked entry never holds up runnable ones behind it.
"""

from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Callable


@dataclass(order=False)
class Entry:
    task_id: str
    priority: str
    deadline: datetime
    seq: int

    def key(self, ranks: dict) -> tuple:
        return (ranks[self.priority], self.deadline, self.seq)


class Scheduler:
    def __init__(self, ranks: dict):
        self._ranks = ranks
        self._lock = threading.Lock()
        self._seq = itertools.count()
        self._entries: dict[str, Entry] = {}

    def push(self, task_id: str, priority: str, deadline: datetime) -> None:
        with self._lock:
            if task_id not in self._entries:
                self._entries[task_id] = Entry(task_id, priority, deadline, next(self._seq))

    def remove(self, task_id: str) -> None:
        with self._lock:
            self._entries.pop(task_id, None)

    def ordered(self) -> list[Entry]:
        with self._lock:
            return sorted(self._entries.values(), key=lambda e: e.key(self._ranks))

    def __contains__(self, task_id: str) -> bool:
        return task_id in self._entries

    def next_runnable(self, can_run: Callable[[Entry], bool]) -> Entry | None:
        for e in self.ordered():
            if can_run(e):
                return e
        return None
