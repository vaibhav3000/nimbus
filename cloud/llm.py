"""LLM access behind one interface, with these implementations:

* RuleBasedLLM    - deterministic offline stand-in that behaves like a
  tool-using model: it reads the pack's rules and the same Context a real
  model reads as text, and answers with tool calls. Default for L0.
* BedrockLLM      - Amazon Bedrock Converse API with forced tool use (L1/L2).
* CircuitBreakerLLM - after repeated failures, fail fast for a cool-down, so a
  Bedrock outage costs one slow request, not one per request (FR-18).
* CassetteLLM     - record/replay, so CI and reruns cost nothing and an
  experiment re-runs byte-identically (NFR-6).

All return LLMResult with token usage, which feeds the PRD cost formula.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import nlp, rules
from .allocator import best_option
from .clock import Clock
from .context import Context
from .pack import Pack
from .topology import Topology


class LLMError(Exception):
    """Model unavailable, throttled, timed out, or answered unusably."""


@dataclass
class LLMResult:
    tool_calls: list = field(default_factory=list)  # [{"name", "input"}]
    text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    model_id: str = ""
    latency_ms: int = 0   # telemetry only: never written to the decision log (NFR-6)
    estimated: bool = False  # True when tokens are a len//4 estimate


def _est_tokens(s: str) -> int:
    return max(1, len(s) // 4)


class RuleBasedLLM:
    """Offline model stand-in. Honest about being one: usage is estimated and
    the model id is 'rule-based-stand-in'."""

    model_id = "rule-based-stand-in"

    def __init__(self, pack: Pack, topo: Topology):
        self.pack, self.topo = pack, topo

    def converse(self, *, system: str, messages: list, tools: list, tool_choice=None,
                 context: Context | None = None, fleet_view=None) -> LLMResult:
        t0 = time.perf_counter()
        prompt = system + "\n" + "\n".join(m["text"] for m in messages)
        call = self._allocate(messages, fleet_view) if "propose_allocation" in tools else self._intent(messages)
        out = json.dumps(call)
        return LLMResult(
            tool_calls=[call], input_tokens=_est_tokens(prompt), output_tokens=_est_tokens(out),
            model_id=self.model_id, latency_ms=int((time.perf_counter() - t0) * 1000), estimated=True)

    def _intent(self, messages: list) -> dict:
        user_turns = [m["text"] for m in messages if m["role"] == "user"]
        request = user_turns[0]
        answer = user_turns[1] if len(user_turns) > 1 else None
        p = nlp.parse(self.pack, self.topo, request, answer=answer)
        if p.missing:
            return {"name": "ask_clarification", "input": nlp.missing_question(self.pack, p)}
        spec = nlp.spec_from(p, note="offline model stand-in")
        facts = {"spec": spec, "dest": self.topo.facts_for(spec["destination"]),
                 "source": self.topo.facts_for(spec.get("source")), "priority": spec["priority"]}
        for v in rules.apply_rules(self.pack.rules, facts).violations:
            if v["kind"] == "clarify":  # a model reads the rule in its context and asks
                return {"name": "ask_clarification", "input": v["question"]}
        return {"name": "submit_task_spec", "input": spec}

    def _allocate(self, messages: list, fleet_view) -> dict:
        spec = json.loads(messages[-1]["text"].split("TASK_SPEC:", 1)[1].strip().splitlines()[0])
        opt = best_option(spec, fleet_view)
        if opt is None:
            return {"name": "propose_allocation",
                    "input": {"cluster_id": "none", "robot_ids": ["none"], "rationale": "no eligible cluster"}}
        return {"name": "propose_allocation", "input": {
            "cluster_id": opt.cluster_id, "robot_ids": [opt.robot_id],
            "rationale": f"{opt.robot_id} is nearest free robot in {opt.cluster_id}"}}


class BedrockLLM:
    """Amazon Bedrock Converse API with forced tool use.

    boto3 is imported lazily so L0 needs no AWS SDK. `model_id` may be a model
    id or a cross-region inference profile id (e.g. apac.*), whichever the
    account can invoke in its region. Timeouts are short on purpose: a request
    must still become a lease within 30 s (NFR-1) when Bedrock is slow.
    """

    def __init__(self, model_id: str, registry, region: str = "ap-south-1", *, client=None,
                 max_tokens: int = 600, temperature: float = 0.0, read_timeout_s: int = 10):
        self.model_id = model_id
        self.registry = registry
        self.max_tokens = max_tokens
        self.temperature = temperature
        if client is None:
            import boto3
            from botocore.config import Config

            client = boto3.client("bedrock-runtime", region_name=region,
                                  config=Config(connect_timeout=3, read_timeout=read_timeout_s,
                                                retries={"max_attempts": 1}))
        self.client = client

    def converse(self, *, system: str, messages: list, tools: list, tool_choice=None,
                 context=None, fleet_view=None) -> LLMResult:
        t0 = time.perf_counter()
        try:
            resp = self.client.converse(
                modelId=self.model_id,
                system=[{"text": system}],
                messages=[{"role": m["role"], "content": [{"text": m["text"]}]} for m in messages],
                toolConfig=self.registry.bedrock_config(tools),
                inferenceConfig={"maxTokens": self.max_tokens, "temperature": self.temperature},
            )
        except Exception as exc:  # throttling, timeout, access denied: all mean "model unavailable"
            raise LLMError(f"bedrock call failed: {exc}") from exc
        blocks = resp["output"]["message"]["content"]
        calls = [{"name": b["toolUse"]["name"], "input": b["toolUse"]["input"]} for b in blocks if "toolUse" in b]
        usage = resp.get("usage", {})
        return LLMResult(
            tool_calls=calls, text=" ".join(b.get("text", "") for b in blocks if "text" in b),
            input_tokens=usage.get("inputTokens", 0), output_tokens=usage.get("outputTokens", 0),
            model_id=self.model_id, latency_ms=int((time.perf_counter() - t0) * 1000))


class CircuitBreakerLLM:
    """Closed -> open after `failures` consecutive errors; while open every call
    fails immediately with LLMError (so the deterministic path takes over with
    no wait); after `cooldown_s` one trial call is let through (half-open)."""

    def __init__(self, inner, clock: Clock, *, failures: int = 3, cooldown_s: float = 30.0):
        self.inner, self.clock = inner, clock
        self.failures, self.cooldown_s = failures, cooldown_s
        self.model_id = getattr(inner, "model_id", "?")
        self._streak = 0
        self._opened_at = None

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        return "half_open" if (self.clock.now() - self._opened_at).total_seconds() >= self.cooldown_s else "open"

    def converse(self, **kw):
        if self.state == "open":
            raise LLMError("circuit open: the model is being skipped after repeated failures")
        try:
            res = self.inner.converse(**kw)
        except LLMError:
            self._streak += 1
            if self._streak >= self.failures or self.state == "half_open":
                self._opened_at = self.clock.now()
            raise
        self._streak, self._opened_at = 0, None
        return res


class CassetteLLM:
    """Record real model calls once, replay them forever (free, deterministic).

    mode 'record' forwards to the wrapped model and stores each result keyed by
    a hash of (system, messages, tools); 'replay' answers from the file and
    raises LLMError on a miss, so a changed prompt is noticed instead of
    silently hitting a paid API. Latency is not replayed: it is telemetry.
    """

    def __init__(self, inner, path: Path, mode: str = "replay"):
        self.inner, self.path, self.mode = inner, Path(path), mode
        self.model_id = getattr(inner, "model_id", "cassette") if inner else "cassette"
        self.tape: dict = json.loads(self.path.read_text()) if self.path.exists() else {}

    @staticmethod
    def _key(system, messages, tools) -> str:
        system = re.sub(r"as of \S+", "as of <t>", system)  # the clock must not change the key
        blob = json.dumps([system, messages, sorted(tools)], sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()[:20]

    def converse(self, *, system, messages, tools, tool_choice=None, context=None, fleet_view=None):
        key = self._key(system, messages, tools)
        if self.mode == "replay" or key in self.tape and self.mode != "record":
            if key not in self.tape:
                raise LLMError(f"cassette miss {key}: prompt changed since recording")
            return LLMResult(**{**self.tape[key], "latency_ms": 0})
        res = self.inner.converse(system=system, messages=messages, tools=tools,
                                  tool_choice=tool_choice, context=context, fleet_view=fleet_view)
        self.tape[key] = res.__dict__
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.tape, indent=1))
        return res
