"""Metrics (context DEC-08; PRD CloudWatch component): counters and latency percentiles, a JSON `GET /metrics`, and
CloudWatch Embedded Metric Format lines (one JSON line on stdout that CloudWatch Logs turns into metrics, with no
extra permission and no agent). Request-to-lease time here is what NFR-1 (<= 30 s p95) is measured from.
"""

from __future__ import annotations

import json
import threading
from collections import Counter, defaultdict, deque


def percentile(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    k = (len(xs) - 1) * p / 100
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (k - lo), 4)


class Metrics:
    def __init__(self, window: int = 2000):
        self._lock = threading.Lock()
        self.counters: Counter = Counter()
        self._series: dict[str, deque] = defaultdict(lambda: deque(maxlen=window))

    def inc(self, name: str, n: float = 1) -> None:
        with self._lock:
            self.counters[name] += n

    def observe(self, name: str, value: float) -> None:
        with self._lock:
            self._series[name].append(value)

    def snapshot(self) -> dict:
        with self._lock:
            series = {k: list(v) for k, v in self._series.items()}
            counters = dict(self.counters)
        return {"counters": counters,
                "latency": {k: {"n": len(v), "p50": percentile(v, 50), "p95": percentile(v, 95), "max": max(v)} for k, v in series.items() if v}}


def emf_line(namespace: str, dimensions: dict, values: dict, now_ms: int) -> str:
    """One CloudWatch Embedded Metric Format record: `values` is {metric: (value, unit)}."""
    return json.dumps({
        "_aws": {"Timestamp": now_ms, "CloudWatchMetrics": [{"Namespace": namespace, "Dimensions": [sorted(dimensions)],
                                                             "Metrics": [{"Name": k, "Unit": u} for k, (v, u) in values.items()]}]},
        **dimensions, **{k: v for k, (v, u) in values.items()}})
