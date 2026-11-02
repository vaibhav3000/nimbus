"""MS2 Task 2: the bounded agent loop and its look-up tools."""

import json

from cloud.agent import run_agent
from cloud.llm import BedrockLLM, CassetteLLM, LLMError, LLMResult
from cloud.readtools import ReadTools
from cloud.tools import ToolRegistry
from cloud_helpers import make_orch, tool

TERMINAL = {"replan", "no_action"}


class Script:
    """An LLM that plays back a list of results (or raises)."""
    model_id = "script"

    def __init__(self, *steps):
        self.steps, self.seen = list(steps), []

    def converse(self, **kw):
        self.seen.append(kw["messages"])
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def parts():
    o = make_orch()
    read = ReadTools(topo=o.topo, fleet=o.fleet, policies=o.policies, mapstate=o.mapstate, clock=o.clock)
    return o, read, ToolRegistry(o.pack)


def test_loop_feeds_a_read_result_back_then_accepts_the_terminal_call():
    o, read, reg = parts()
    llm = Script(tool("get_route", src="E-STORE", dst="2B", avoid=["E3"]),
                 tool("replan", avoid_zones=["E3"], rationale="E4 is open"))
    res = run_agent(llm, system="s", user="E3 is blocked", tools=["get_route", "replan"], registry=reg,
                    read=read, terminal=TERMINAL)
    assert res.call["name"] == "replan" and res.calls == 2 and res.error is None
    assert res.trace[0]["result"]["path"] == ["E-STORE", "E1", "E2", "E4", "2B"]
    second = llm.seen[1]                                  # the model saw its own call and the result
    assert second[1]["tool_calls"][0]["name"] == "get_route" and second[2]["tool_results"][0]["content"]["path"]


def test_turn_cap_returns_an_error_not_an_exception():
    o, read, reg = parts()
    llm = Script(*[tool("get_fleet") for _ in range(3)])
    res = run_agent(llm, system="s", user="u", tools=["get_fleet", "replan"], registry=reg, read=read,
                    terminal=TERMINAL, max_turns=3)
    assert res.call is None and "3 turns" in res.error and res.calls == 3


def test_a_rejected_terminal_call_gets_one_more_turn_with_the_reason():
    o, read, reg = parts()
    llm = Script(tool("replan", avoid_zones=["nowhere"], rationale="x"),
                 tool("replan", avoid_zones=["E3"], rationale="ok"))
    res = run_agent(llm, system="s", user="u", tools=["replan"], registry=reg, read=read, terminal=TERMINAL,
                    validate=lambda c: [f"unknown zone {z}" for z in c["input"]["avoid_zones"] if z not in o.topo.nodes])
    assert res.call["input"]["avoid_zones"] == ["E3"]
    assert "unknown zone nowhere" in llm.seen[1][2]["tool_results"][0]["content"]["error"]


def test_model_error_ends_the_run_cleanly():
    o, read, reg = parts()
    res = run_agent(Script(LLMError("throttled")), system="s", user="u", tools=["replan"], registry=reg,
                    read=read, terminal=TERMINAL)
    assert res.call is None and res.error == "throttled"


def test_read_tools_do_not_change_anything():
    o, read, _ = parts()
    before = json.dumps([o.fleet.snapshot(), o.mapstate.closed(o.clock.now()), o.repo.live_leases()], sort_keys=True, default=str)
    for name, args in [("get_fleet", {}), ("get_stock", {}), ("get_route", {"src": "E1", "dst": "2B"}),
                       ("get_policies", {"query": "paediatric pump"}), ("get_task_log", {"task_id": "t"}), ("get_map", {})]:
        read.run(name, args)
    assert before == json.dumps([o.fleet.snapshot(), o.mapstate.closed(o.clock.now()), o.repo.live_leases()], sort_keys=True, default=str)


def test_read_tool_results_are_capped_and_unknown_zone_is_an_error_value():
    o, read, _ = parts()
    assert "error" in read.run("get_route", {"src": "nope", "dst": "2B"})
    assert len(json.dumps(read.run("get_policies", {"query": "pump isolation gate speed urgent paediatric"}))) < 2600


def test_bedrock_message_mapping_for_tool_turns():
    seen = {}

    class Client:
        def converse(self, **kw):
            seen.update(kw)
            return {"output": {"message": {"content": [{"toolUse": {"toolUseId": "u2", "name": "replan",
                                                                    "input": {"avoid_zones": ["E3"], "rationale": "r"}}}]}},
                    "usage": {"inputTokens": 1, "outputTokens": 1}}

    _, _, reg = parts()
    llm = BedrockLLM("m", reg, client=Client())
    res = llm.converse(system="s", tools=["replan", "get_fleet"], messages=[
        {"role": "user", "text": "hi"},
        {"role": "assistant", "tool_calls": [{"id": "u1", "name": "get_fleet", "input": {}}]},
        {"role": "user", "tool_results": [{"id": "u1", "content": {"clusters": []}}]}])
    assert seen["messages"][1]["content"][0]["toolUse"]["toolUseId"] == "u1"
    assert seen["messages"][2]["content"][0]["toolResult"]["content"] == [{"json": {"clusters": []}}]
    assert res.tool_calls[0]["id"] == "u2"


def test_cassette_replays_a_multi_turn_run(tmp_path):
    o, read, reg = parts()
    path = tmp_path / "c.json"
    steps = [tool("get_fleet"), tool("no_action", rationale="fine")]
    rec = CassetteLLM(Script(*steps), path, "record")
    first = run_agent(rec, system="s", user="u", tools=["get_fleet", "no_action"], registry=reg, read=read, terminal=TERMINAL)
    play = CassetteLLM(None, path, "replay")
    again = run_agent(play, system="s", user="u", tools=["get_fleet", "no_action"], registry=reg, read=read, terminal=TERMINAL)
    assert again.call == first.call and again.calls == 2
