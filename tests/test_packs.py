"""Domain Packs: the generality guarantee. The same core runs two different
domains from data alone; bad packs fail at load; no domain word may live in
the core; and a run is a deterministic function of its inputs (NFR-6)."""

import copy
import json
import re
import shutil
from pathlib import Path

import pytest

from cloud import rules
from cloud.clock import FixedClock
from cloud.context import summarise_log
from cloud.llm import CircuitBreakerLLM, LLMError
from cloud.pack import PACKS_DIR, Pack, PackError
from cloud.scheduler import Scheduler
from cloud.tools import ToolRegistry
from cloud_helpers import make_orch, new_task
from datetime import timedelta

PACK_NAMES = ["hospital", "rescue"]
SCENARIOS = [(n, s) for n in PACK_NAMES for s in Pack.load(n).scenarios]


# -- golden scenarios, per pack, through the one orchestrator --------------------------------------

def run_scenario(pack_name, sc):
    orch = make_orch(pack=Pack.load(pack_name))
    for zone, kind in sc.get("setup", {}).get("restrict", []):
        orch.policies.declare(zone, kind)
    task = new_task()
    text, question, out = None, None, None
    for turn in sc["turns"]:
        if "say" in turn:
            text = turn["say"]
            out = orch.handle_request(task, text)
        else:
            out = orch.handle_request(task, text, question=question, answer=turn["answer"])
        question = out.question
        check_expect(out, turn["expect"], sc["id"])
    return out


def check_expect(out, exp, sid):
    assert out.kind == exp["kind"], f"{sid}: expected {exp['kind']}, got {out.kind} ({out.reason or out.question})"
    if "question_contains" in exp:
        assert exp["question_contains"] in out.question["question"]
    if "reason_contains" in exp:
        assert exp["reason_contains"] in out.reason
    if out.kind != "leased":
        return
    lease, spec = out.lease, out.spec
    from contracts import validate_document
    assert validate_document("lease", lease) == []
    if "lease_cluster" in exp:
        assert lease["cluster_id"] == exp["lease_cluster"]
    if "lease_priority" in exp:
        assert lease["priority"] == exp["lease_priority"]
    if "item" in exp:
        assert spec["item"] == exp["item"]
    if "deadline_minutes" in exp:
        assert spec["deadline_minutes"] == exp["deadline_minutes"]
    for z in exp.get("zones_include", []):
        assert z in lease["allowed_zones"], f"{sid}: {z} missing from {lease['allowed_zones']}"
    if "forbidden_zones" in exp:
        assert lease["forbidden_zones"] == exp["forbidden_zones"]
    if "sequence_cmds" in exp:
        assert [s["cmd"] for s in lease["task_sequence"]] == exp["sequence_cmds"]
    gotos = [s["params"]["to"] for s in lease["task_sequence"] if s["cmd"] == "go_to"]
    if "first_goto" in exp:
        assert gotos[0] == exp["first_goto"]
    if "last_goto" in exp:
        assert gotos[-1] == exp["last_goto"]
    for k, v in exp.get("spec_field", {}).items():
        assert spec.get(k) == v


@pytest.mark.parametrize("pack_name,sc", SCENARIOS, ids=[f"{n}:{s['id']}" for n, s in SCENARIOS])
def test_golden_scenarios(pack_name, sc):
    run_scenario(pack_name, sc)


def test_the_two_packs_really_differ_but_share_one_core():
    h, r = Pack.load("hospital"), Pack.load("rescue")
    th, tr = ToolRegistry(h), ToolRegistry(r)
    assert "infusion_pump" in th.schema("submit_task_spec")["properties"]["item"]["enum"]
    assert th.schema("submit_task_spec")["properties"]["item"]["enum"] != tr.schema("submit_task_spec")["properties"]["item"]["enum"]
    assert tr.schema("submit_task_spec")["properties"]["priority"]["enum"] == ["critical", "high", "normal"]
    assert "entry_confirmed" in tr.schema("submit_task_spec")["properties"]
    assert "variant_confirmed_by_requester" not in tr.schema("submit_task_spec")["properties"]
    ctx_r = make_orch(pack=r).build_context("survey sector 2", "task-000001", []).to_prompt()
    assert "SEC-2 (area alpha, UNSTABLE)" in ctx_r and "ward" not in ctx_r.lower() and "pack rescue@0.1.0" in ctx_r


def test_three_priority_classes_order_by_pack_rank():
    r = Pack.load("rescue")
    s, t = Scheduler(r.ranks), FixedClock().now()
    s.push("n", "normal", t + timedelta(minutes=1))
    s.push("h", "high", t + timedelta(minutes=50))
    s.push("c", "critical", t + timedelta(minutes=90))
    assert [e.task_id for e in s.ordered()] == ["c", "h", "n"]


# -- the guard against hardcoding creeping back ---------------------------------------------------------

DOMAIN_WORDS = re.compile(
    r"\b(paediatric|pediatric|infusion|wheelchair|hospital|wards?|beds?|patients?|nurses?|isolation|"
    r"wing|east-wing|west-wing|pumps?|SOP-[A-Z]+-\d+|stat)\b", re.IGNORECASE)


def test_core_contains_no_domain_vocabulary():
    """Everything about an application domain lives in packs/. If this fails, a
    hospital (or any domain) assumption has leaked back into cloud/*.py."""
    hits = []
    for f in sorted(Path("cloud").glob("*.py")):
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if DOMAIN_WORDS.search(line):
                hits.append(f"{f}:{n}: {line.strip()}")
    assert not hits, "domain words in the core:\n" + "\n".join(hits)


# -- pack validation: bad packs fail at load, naming every problem -------------------------------------------

@pytest.fixture()
def broken(tmp_path):
    def make(mutate, topo=None):
        root = tmp_path / "p"
        if root.exists():
            shutil.rmtree(root)
        shutil.copytree(PACKS_DIR / "hospital", root)
        doc = json.loads((root / "pack.json").read_text())
        mutate(doc)
        (root / "pack.json").write_text(json.dumps(doc))
        return root
    return make


@pytest.mark.parametrize("mutate,needle", [
    (lambda d: d["priorities"][0].update(lease_priority="stat"), "not in the lease contract enum"),
    (lambda d: d["task_templates"]["delivery"][0].update(cmd="teleport"), "not a robot sub-task command"),
    (lambda d: d["prompts"].update(intent="prompts/nope.txt"), "file missing or empty"),
    (lambda d: d["items"][1].update(patterns=["(unclosed"]), "bad regex"),
    (lambda d: d["rules"][0]["question"]["readings"][0]["set"].update(item="unicorn"), "sets unknown item"),
    (lambda d: d["restrictions"]["isolation"].update(policy_id="NOPE"), "is not in policies.json"),
    (lambda d: d["restrictions"]["isolation"].update(exempt_priorities=["vip"]), "exempt priority"),
    (lambda d: d["ontology"].update(destination_kinds=["moat"]), "no node in topology.json"),
    (lambda d: d["steps"].update(intent=["launch_missiles"]), "unknown tool"),
    (lambda d: d.update(default_priority="vip"), "not a priority"),
    (lambda d: d["priorities"][1].update(rank=0), "unique"),
    (lambda d: d.pop("rules"), "'rules' is a required property"),
])
def test_bad_packs_fail_at_load_with_a_clear_reason(broken, mutate, needle):
    with pytest.raises(PackError) as exc:
        Pack(broken(mutate))
    assert needle in str(exc.value)


def test_pack_id_changes_when_any_pack_file_changes(broken):
    base = Pack(broken(lambda d: None)).id
    assert base == Pack(broken(lambda d: None)).id
    assert base != Pack(broken(lambda d: d.update(description="changed"))).id


# -- rule engine -----------------------------------------------------------------------------------------------

def test_rule_conditions_are_safe_data_not_code():
    facts = {"spec": {"item": "a", "n": 3}, "dest": {"flag": True}}
    ev = lambda c: rules.evaluate(c, facts)
    assert ev({"eq": ["$spec.item", "a"]}) and not ev({"eq": ["$spec.item", "b"]})
    assert ev({"in": ["$spec.item", ["a", "b"]]}) and ev({"truthy": "$dest.flag"}) and not ev({"truthy": "$dest.nope"})
    assert ev({"all": [{"gt": ["$spec.n", 2]}, {"not": {"eq": ["$spec.n", 4]}}]}) and ev({"any": [{"lt": ["$spec.n", 1]}, {"ge": ["$spec.n", 3]}]})
    assert not ev({"gt": ["$spec.missing", 1]})            # a missing path never raises
    with pytest.raises(ValueError):
        ev({"exec": "__import__('os')"})                    # unknown operators are refused, nothing is executed
    assert rules.render("{dest.zone} is {dest.what}", {"dest": {"zone": "Z"}}) == "Z is "


# -- context: the task log carries the consequences of earlier actions ---------------------------------------------

def test_task_log_summarises_consequences_not_conversation():
    rows = [
        {"who": "requester", "output": {"event": "request_received"}, "inputs": {"text": "x"}, "lease_version": None, "timestamp": "2026-10-18T09:00:00Z"},
        {"who": "cloud", "output": {"decision": "propose_lease", "cluster": "c"}, "inputs": {}, "lease_version": None, "timestamp": "2026-10-18T09:00:01Z"},
        {"who": "cloud", "output": {"decision": "issue_lease", "lease_id": "lease-t-v1"}, "inputs": {}, "lease_version": 1, "timestamp": "2026-10-18T09:00:02Z"},
        {"who": "edge", "output": {"event": "UNEXPECTED_EVENT"}, "inputs": {"what_happened": "cart on E3", "where": "E3"}, "lease_version": 1, "timestamp": "2026-10-18T09:03:00Z"},
        {"who": "cloud", "output": {"decision": "replan", "zones": ["E4"]}, "inputs": {"blocked_zones": ["E3"]}, "lease_version": None, "timestamp": "2026-10-18T09:03:01Z"},
    ]
    lines = summarise_log(rows)
    assert len(lines) == 3                      # request, proposal bookkeeping: skipped
    assert "cloud: issue_lease v1 (lease_id=lease-t-v1)" in lines[0]
    assert "edge: UNEXPECTED_EVENT v1" in lines[1] and "what_happened=cart on E3" in lines[1]
    assert "blocked_zones" in lines[2]


def test_the_models_prompt_includes_the_task_log_and_restrictions():
    orch = make_orch()
    orch.policies.declare("2C", "isolation")
    rows = [{"who": "edge", "output": {"event": "checkin"}, "inputs": {}, "lease_version": 1, "timestamp": "2026-10-18T09:00:09Z"}]
    prompt = orch.build_context("blanket to 2C", "task-000001", [], rows).to_prompt()
    assert "Task log so far" in prompt and "edge: checkin v1" in prompt
    assert "2C: hold by SOP-ISO-02, exempt: urgent" in prompt


# -- NFR-6: a run is a deterministic function of its inputs --------------------------------------------------------------

def test_two_identical_runs_produce_byte_identical_decision_rows():
    def run():
        orch = make_orch()
        task = new_task()
        q = orch.handle_request(task, "Need an infusion pump at 2B-12, urgent.")
        out = orch.handle_request(task, "Need an infusion pump at 2B-12, urgent.", question=q.question, answer="paediatric")
        task.update(lease=out.lease, spec=out.spec)
        v2 = orch.replan(task, blocked_zones=["E3"], reason="cart")
        return json.dumps([q.rows, out.rows, v2.rows], sort_keys=True)
    first, second = run(), run()
    assert first == second
    assert "latency" not in first                # timing is telemetry, never part of the log


def test_timings_are_reported_separately_from_the_log():
    out = make_orch().handle_request(new_task(), "wheelchair to ward 3")
    assert "llm_ms" in out.timings and out.timings["llm_ms"] >= 0


# -- circuit breaker (FR-18: an outage must not cost every request a timeout) ----------------------------------------------

class Flaky:
    model_id = "flaky"

    def __init__(self):
        self.calls, self.fail = 0, True

    def converse(self, **kw):
        self.calls += 1
        if self.fail:
            raise LLMError("boom")
        return "ok"


def test_circuit_opens_after_repeated_failures_and_half_opens_after_cooldown():
    clock, inner = FixedClock(), Flaky()
    cb = CircuitBreakerLLM(inner, clock, failures=3, cooldown_s=30)
    for _ in range(3):
        with pytest.raises(LLMError, match="boom"):
            cb.converse()
    assert cb.state == "open" and inner.calls == 3
    with pytest.raises(LLMError, match="circuit open"):
        cb.converse()
    assert inner.calls == 3                       # failed fast: the model was not called
    clock.advance(31)
    assert cb.state == "half_open"
    inner.fail = False
    assert cb.converse() == "ok" and cb.state == "closed"


def test_a_failed_trial_call_reopens_the_circuit_at_once():
    clock, inner = FixedClock(), Flaky()
    cb = CircuitBreakerLLM(inner, clock, failures=2, cooldown_s=10)
    for _ in range(2):
        with pytest.raises(LLMError):
            cb.converse()
    clock.advance(11)
    with pytest.raises(LLMError, match="boom"):
        cb.converse()
    assert cb.state == "open"


def test_during_an_outage_new_requests_still_get_a_lease_from_the_rules_only():
    """PRD FR-18: no lease from the LLM path during the outage; the deterministic path still serves."""
    clock = FixedClock()
    orch = make_orch(clock=clock)
    inner = Flaky()
    orch.llm = CircuitBreakerLLM(inner, clock, failures=1, cooldown_s=600)
    outs = [orch.handle_request(new_task(i), "Deliver a wheelchair to ward 3, routine.") for i in (1, 2, 3)]
    assert all(o.kind == "leased" and "Reduced autonomy" in o.notice for o in outs)
    assert all(o.rows[0]["output"]["proposed_by"] == "classical" for o in outs)
    assert inner.calls == 1                       # only the first request paid for a failing call
