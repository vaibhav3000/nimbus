"""A bounded agent loop: the model may call look-up tools, then must end with
one terminal choice. Code validates every step.

* a look-up tool runs, its result goes back to the model, the loop continues;
* a terminal tool call is checked by `validate`; if it is rejected the model is
  told why and gets another turn;
* the turn cap, a model error or repeated rejection ends the run with
  `call=None` and an `error`, so the caller falls back to deterministic rules
  (FR-15/FR-18). The loop never raises for model trouble.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .llm import LLMError


@dataclass
class AgentResult:
    call: dict | None = None
    trace: list = field(default_factory=list)   # [{"turn","tool","input","result"}] for the decision log
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    error: str | None = None


def run_agent(llm, *, system: str, user: str, tools: list[str], registry, read, terminal: set[str],
              validate: Callable[[dict], list[str]] | None = None, max_turns: int = 4,
              context=None, fleet_view=None) -> AgentResult:
    out = AgentResult()
    messages: list[dict] = [{"role": "user", "text": user}]
    last = "no tool call"
    for turn in range(max_turns):
        try:
            res = llm.converse(system=system, messages=messages, tools=tools, tool_choice="any",
                               context=context, fleet_view=fleet_view)
        except LLMError as exc:
            out.error = str(exc)
            return out
        out.calls += 1
        out.tokens_in += res.input_tokens
        out.tokens_out += res.output_tokens
        call = res.tool_calls[0] if res.tool_calls else None
        if call is None:
            messages.append({"role": "user", "text": "Call one of the tools."})
            continue
        cid = call.get("id") or f"turn{turn}"
        messages.append({"role": "assistant", "tool_calls": [{"id": cid, "name": call["name"], "input": call["input"]}]})
        errs = registry.validate(call["name"], call["input"])
        if not errs and call["name"] in terminal and validate:
            errs = validate(call)
        if errs:
            last = "; ".join(errs)
            result = {"error": last}
        elif call["name"] in terminal:
            out.call = {"name": call["name"], "input": call["input"]}
            return out
        else:
            result = read.run(call["name"], call["input"])
        out.trace.append({"turn": turn, "tool": call["name"], "input": call["input"], "result": result})
        messages.append({"role": "user", "tool_results": [{"id": cid, "content": result}]})
    out.error = f"no accepted answer in {max_turns} turns: {last}"
    return out
