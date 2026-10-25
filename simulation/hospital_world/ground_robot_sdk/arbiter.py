"""Small lease-based motion-source arbiter."""
from __future__ import annotations
import time


class CommandArbiter:
    PRIORITY={"keyboard":10, "remote":20, "autonomy":30, "safety":90, "emergency":100}
    def __init__(self): self._active: tuple[str, float] | None=None
    def authorize(self, source: str, lease: float) -> tuple[bool, str | None]:
        now=time.monotonic(); current=self._active
        if current and current[1] <= now: self._active=None; current=None
        priority=self.PRIORITY.get(source, 0)
        if current and current[0] != source and self.PRIORITY.get(current[0],0) > priority:
            return False, f"{current[0]} currently has higher-priority control"
        self._active=(source, now+max(lease, .01)); return True, None
    def release(self, source: str) -> None:
        if self._active and self._active[0] == source: self._active=None
    @property
    def active_source(self):
        if self._active and self._active[1] <= time.monotonic(): self._active=None
        return None if self._active is None else self._active[0]
