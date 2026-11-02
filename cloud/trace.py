"""A trace of what happens for an input: every message between requester, cloud, model and edge, in order,
with wall-clock time. For understanding and demos only: it is memory-bounded, never feeds a decision, and
is separate from the decision log (which stays the durable, replayable record, FR-20).
"""

from __future__ import annotations

import contextvars
import itertools
import threading
import time
from collections import deque
from datetime import datetime, timezone

_current_task: contextvars.ContextVar[str | None] = contextvars.ContextVar("trace_task", default=None)


def task_scope(task_id: str | None):
    """Context manager: model calls made inside it are attributed to this task."""
    class _Scope:
        def __enter__(self):
            self.token = _current_task.set(task_id)

        def __exit__(self, *exc):
            _current_task.reset(self.token)
    return _Scope()


class Trace:
    def __init__(self, capacity: int = 2000):
        self._events: deque = deque(maxlen=capacity)
        self._seq = itertools.count(1)
        self._lock = threading.Lock()

    def add(self, src: str, dst: str, kind: str, summary: str, *, task_id: str | None = None, detail: dict | None = None) -> None:
        now = time.time()
        with self._lock:
            self._events.append({
                "seq": next(self._seq), "t": now,
                "ts": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "from": src, "to": dst, "kind": kind, "summary": summary,
                "task_id": task_id or _current_task.get(), "detail": detail or {}})

    def events(self, task_id: str | None = None, after: int = 0, limit: int = 400) -> list[dict]:
        with self._lock:
            out = [dict(e) for e in self._events if e["seq"] > after and (task_id is None or e["task_id"] == task_id)]
        return out[-limit:]


def _step_of(tools: list[str]) -> str:
    names = set(tools)
    for step, marker in (("decide_step", "decide_step"), ("react", "replan"), ("react-clarify", "ask_requester"),
                         ("allocation", "propose_allocation"), ("intent", "submit_task_spec")):
        if marker in names:
            return step
    return "model"


class TracingLLM:
    """Wraps any model object and records each call: what it was asked, what it answered, how long it took."""

    def __init__(self, inner, trace: Trace):
        self.inner, self.trace = inner, trace

    def __getattr__(self, name):          # tests and the cost code reach through to the wrapped model
        return getattr(self.inner, name)

    def converse(self, **kw):
        step = _step_of(kw.get("tools", []))
        last = kw["messages"][-1]
        asked = last.get("text") or ("tool results" if last.get("tool_results") else "tool call")
        t0 = time.perf_counter()
        try:
            res = self.inner.converse(**kw)
        except Exception as exc:
            self.trace.add("cloud", "model", "llm-error", f"{step}: {type(exc).__name__}: {exc}",
                           detail={"step": step, "tools": kw.get("tools", [])})
            raise
        ms = int((time.perf_counter() - t0) * 1000)
        calls = [{"name": c["name"], "input": c["input"]} for c in res.tool_calls]
        self.trace.add("cloud", "model", "llm", f"{step}: {', '.join(c['name'] for c in calls) or 'no tool call'}  "
                       f"({res.input_tokens} in / {res.output_tokens} out tokens, {ms} ms)",
                       detail={"step": step, "tools": kw.get("tools", []), "asked": asked[:600],
                               "system_chars": len(kw.get("system", "")), "answered": calls,
                               "tokens_in": res.input_tokens, "tokens_out": res.output_tokens, "ms": ms,
                               "model": res.model_id, "estimated": res.estimated})
        return res


# -- wiring into the storage layer: every outgoing message and every decision-log row becomes a trace event ----------

def _lease_summary(p: dict) -> str:
    steps = " > ".join(s["cmd"] + (f"({s['params']['to']})" if s["params"].get("to") else "") for s in p["task_sequence"])
    kind = "STOP lease" if [s["cmd"] for s in p["task_sequence"]] == ["stop"] else "LEASE"
    return (f"{kind} v{p['version']} {','.join(p['assigned_robots'])} [{p['priority']}] {steps}"
            f" | allowed {len(p['allowed_zones'])} zones, forbidden {p['forbidden_zones'] or 'none'}, expires {p['expiry_ts']}")


def describe_outbox(topic: str, payload: dict) -> tuple[str, str, str | None]:
    """(kind, summary, task_id) for a message the cloud publishes toward an edge."""
    kind = topic.rsplit("/", 1)[-1]
    if kind == "lease":
        return "lease", _lease_summary(payload), payload["lease_id"][len("lease-"):payload["lease_id"].rindex("-v")]
    if kind == "renewal":
        return "renewal", f"RENEWAL {payload['lease_id']} now expires {payload['expiry_ts']}", None
    if kind == "probe":
        return "probe", "PROBE: are you still there?", None
    return "notice", f"NOTICE [{payload.get('type')}] {payload.get('text')}", payload.get("task_id")


def describe_row(row: dict) -> tuple[str, str, str]:
    """(from, to, summary) for a decision-log row."""
    out, who = row["output"], row["who"]
    what = out.get("decision") or out.get("event") or "?"
    bits = []
    for key in ("action", "source", "cluster", "to_cluster", "zone", "kind", "reason", "text", "question", "why"):
        v = out.get(key)
        if v not in (None, "", []):
            bits.append(f"{key}={v if not isinstance(v, (dict, list)) else str(v)[:70]}")
    summary = f"{what}" + (f"  ({'; '.join(bits[:3])})" if bits else "")
    return {"requester": ("requester", "cloud"), "edge": ("edge", "cloud")}.get(who, (who, who)) + (summary,)


def wire_store(trace: Trace, store) -> None:
    """Make `store` report to `trace` (the instance's methods are wrapped; the data written is unchanged)."""
    append_outbox, append_decision = store.append_outbox, store.append_decision

    def out(entry: dict) -> None:
        append_outbox(entry)
        kind, summary, task_id = describe_outbox(entry["topic"], entry["payload"])
        cluster = entry["topic"].split("/")[1]
        trace.add("cloud", cluster, kind, summary, task_id=task_id, detail={"topic": entry["topic"], "payload": entry["payload"]})

    def dec(row: dict) -> None:
        append_decision(row)
        src, dst, summary = describe_row(row)
        trace.add(src, dst, "decision", summary, task_id=row.get("task_id"),
                  detail={"who": row["who"], "lease_version": row.get("lease_version"), "inputs": row["inputs"], "output": row["output"]})

    store.append_outbox, store.append_decision = out, dec
