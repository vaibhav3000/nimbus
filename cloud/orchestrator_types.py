"""Types shared by the orchestrator and the modules it delegates to (kept apart to avoid import cycles)."""

from __future__ import annotations

from dataclasses import dataclass, field

LLM_MODES = ("C1", "C2")   # C3 is the no-LLM baseline; C2 differs from C1 only in who decides at the edge


@dataclass
class Outcome:
    kind: str                              # clarify | leased | queued | rejected | replan_failed
    rows: list = field(default_factory=list)   # {"who","inputs","output","lease_version"}
    question: dict | None = None
    lease: dict | None = None
    spec: dict | None = None
    reason: str | None = None
    notice: str | None = None
    publish: list = field(default_factory=list)  # [(topic, payload)]
    tokens_in: int = 0
    tokens_out: int = 0
    llm_calls: int = 0                     # model round trips this decision used (NFR-4)
    reservation: dict | None = None
    timings: dict = field(default_factory=dict)  # telemetry only; never in the decision log


def row(who, inputs, output, version=None) -> dict:
    return {"who": who, "inputs": inputs, "output": output, "lease_version": version}
