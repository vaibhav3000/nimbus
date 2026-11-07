"""Priority and deadline scheduler (FR-14): classes ordered by the pack's
ranks (urgent before routine), earliest deadline first inside a class,
arrival order breaks ties.

It decides *order*, never allocation. A queued task is retried whenever
capacity frees up; a blocked entry never holds up runnable ones behind it.

Starvation guard: a task whose deadline has already passed is ranked with the most urgent class (still earliest
deadline first), so a stream of new urgent requests cannot keep an overdue routine task waiting for ever. Tasks that
are not yet overdue keep the plain order, so urgent work still goes first while there is time. The guard needs a
clock (`now`, set by the Orchestrator); without one the order is the plain one.
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
    tried_epoch: int = -1      # the capacity epoch at which this task last failed to place; retry only when it moved

    def key(self, ranks: dict, now: datetime | None = None) -> tuple:
        rank = ranks[self.priority]
        if now is not None and self.deadline < now:
            rank = min(ranks.values())      # overdue: no longer allowed to wait behind newer urgent work
        return (rank, self.deadline, self.seq)


class Scheduler:
    kind = "edf"    # priority rank, then earliest deadline, then arrival (FR-14)

    def __init__(self, ranks: dict, now: Callable[[], datetime] | None = None):
        self._ranks = ranks
        self.now = now
        self._lock = threading.Lock()
        self._seq = itertools.count()
        self._entries: dict[str, Entry] = {}

    def push(self, task_id: str, priority: str, deadline: datetime) -> None:
        with self._lock:
            if task_id not in self._entries:
                self._entries[task_id] = Entry(task_id, priority, deadline, next(self._seq))

    def mark_tried(self, task_id: str, epoch: int) -> None:
        with self._lock:
            if task_id in self._entries:
                self._entries[task_id].tried_epoch = epoch

    def remove(self, task_id: str) -> None:
        with self._lock:
            self._entries.pop(task_id, None)

    def ordered(self) -> list[Entry]:
        with self._lock:
            now = self.now() if self.now is not None else None
            return sorted(self._entries.values(), key=lambda e: e.key(self._ranks, now))

    def __contains__(self, task_id: str) -> bool:
        return task_id in self._entries

    def next_runnable(self, can_run: Callable[[Entry], bool]) -> Entry | None:
        for e in self.ordered():
            if can_run(e):
                return e
        return None


class FifoScheduler(Scheduler):
    """A deliberately naive classical baseline: arrival order only -- no
    priority class, no deadline.  It is NOT the PRD's C3 (which specifies an
    earliest-deadline-first scheduler); it is a separate, weaker C3 variant
    used to show that a classical baseline's success under contention depends
    heavily on how well it is engineered.  Selected with NIMBUS_SCHEDULER=fifo.

    Under contention it hands a freed robot to whoever arrived first, so an
    urgent request that arrives behind routine ones waits and can miss its
    deadline -- exactly what priority+EDF scheduling (the default) avoids.
    """

    kind = "fifo"

    def ordered(self) -> list[Entry]:
        with self._lock:
            return sorted(self._entries.values(), key=lambda e: e.seq)
