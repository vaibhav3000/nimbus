"""Fault injection for the model (PRD E5: "Bedrock unavailable"; FR-18 acceptance: calls forced to fail for 10 minutes).

`FaultyLLM` wraps any model object. While an outage window is open every call raises LLMError, exactly as a throttled or
unreachable Bedrock does, so the circuit breaker, the rules fallback and the "reduced autonomy" notice are exercised for real.
Windows are measured on the orchestrator's clock, so a test can run ten minutes in microseconds, and a live run can start one
with `POST /llm/outage {"seconds": 600}` or `NIMBUS_LLM_OUTAGE=<after_s>:<duration_s>`.
"""

from __future__ import annotations

import os
from datetime import timedelta

from .llm import LLMError


class FaultyLLM:
    def __init__(self, inner, clock):
        self.inner, self.clock = inner, clock
        self.model_id = getattr(inner, "model_id", "?")
        self.windows: list[tuple] = []     # [(start, end)] datetimes
        self.failed_calls = 0

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def schedule(self, after_s: float, duration_s: float) -> None:
        start = self.clock.now() + timedelta(seconds=after_s)
        self.windows.append((start, start + timedelta(seconds=duration_s)))

    def fail_for(self, seconds: float) -> None:
        """Start an outage now; 0 ends any outage in progress."""
        now = self.clock.now()
        self.windows = [(s, min(e, now)) for s, e in self.windows if s <= now]
        if seconds > 0:
            self.windows.append((now, now + timedelta(seconds=seconds)))

    @property
    def down(self) -> bool:
        now = self.clock.now()
        return any(s <= now < e for s, e in self.windows)

    def converse(self, **kw):
        if self.down:
            self.failed_calls += 1
            raise LLMError("injected outage: the model is unavailable")
        return self.inner.converse(**kw)

    @classmethod
    def from_env(cls, inner, clock) -> "FaultyLLM":
        f = cls(inner, clock)
        spec = os.environ.get("NIMBUS_LLM_OUTAGE")      # "<after_s>:<duration_s>", e.g. "120:600"
        if spec:
            after, _, duration = spec.partition(":")
            f.schedule(float(after), float(duration))
        return f
