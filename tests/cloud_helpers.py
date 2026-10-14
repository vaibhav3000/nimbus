"""Builders for Person 3's tests: an Orchestrator on a fixed clock with a
scriptable LLM, so health, fallback and veto paths are deterministic."""

from datetime import timedelta  # noqa: F401  (re-exported for tests)

from cloud.clock import FixedClock
from cloud.fleet import FleetRegistry
from cloud.leases import MemoryLeaseRepo
from cloud.llm import LLMError, LLMResult, RuleBasedLLM
from cloud.orchestrator import Orchestrator
from cloud.pack import Pack
from cloud.policy import PolicyBook
from cloud.scheduler import Scheduler
from cloud.topology import Topology


class ScriptedLLM:
    """Wraps the rule-based model; `override(tools, kwargs)` may return an
    LLMResult (or raise LLMError) to simulate a misbehaving model."""

    model_id = "scripted"

    def __init__(self, pack, topo, override=None):
        self.inner = RuleBasedLLM(pack, topo)
        self.override = override
        self.calls = []

    def converse(self, **kw):
        self.calls.append(kw["tools"])
        if self.override:
            res = self.override(kw["tools"], kw)
            if res is not None:
                return res
        return self.inner.converse(**kw)


def tool(name, **inp):
    return LLMResult(tool_calls=[{"name": name, "input": inp}], input_tokens=10, output_tokens=5, model_id="scripted")


def make_orch(*, clock=None, override=None, mode="C1", allow_unknown=True, published=None, repo=None, pack=None):
    clock = clock or FixedClock()
    pack = pack or Pack.load("hospital")
    topo = Topology(pack)
    published = published if published is not None else []
    orch = Orchestrator(
        pack=pack, topo=topo, fleet=FleetRegistry.load(clock, pack), policies=PolicyBook(pack),
        repo=repo or MemoryLeaseRepo(), scheduler=Scheduler(pack.ranks), llm=ScriptedLLM(pack, topo, override),
        clock=clock, mode=mode, allow_unknown=allow_unknown,
        publish=lambda topic, payload: published.append((topic, payload)))
    return orch


def new_task(n=1, text="x"):
    return {"task_id": f"task-{n:06d}", "request_text": text, "status": "received"}
