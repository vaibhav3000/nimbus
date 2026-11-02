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


# -- batch 1.5: several tool calls per turn, new look-ups, new final choices, precomputed options -------------------------------

from cloud_helpers import new_task  # noqa: E402


class Multi(Script):
    """A model that answers a turn with several tool calls at once."""


def multi(*calls):
    return LLMResult(tool_calls=[{"id": f"c{i}", "name": n, "input": a} for i, (n, a) in enumerate(calls)],
                     input_tokens=10, output_tokens=5, model_id="script")


def test_look_ups_asked_together_are_answered_together_in_one_turn():
    o, read, reg = parts()
    llm = Script(multi(("get_fleet", {}), ("get_route", {"src": "E1", "dst": "2B"}), ("get_map", {})),
                 tool("no_action", rationale="fine"))
    res = run_agent(llm, system="s", user="u", tools=["get_fleet", "get_route", "get_map", "no_action"], registry=reg, read=read, terminal=TERMINAL)
    assert res.call["name"] == "no_action" and res.calls == 2
    results = llm.seen[1][2]["tool_results"]
    assert [r["id"] for r in results] == ["c0", "c1", "c2"] and results[1]["content"]["path"]


def test_only_one_final_choice_is_taken_per_turn():
    o, read, reg = parts()
    llm = Script(multi(("replan", {"avoid_zones": ["E3"], "rationale": "a"}), ("no_action", {"rationale": "b"})))
    res = run_agent(llm, system="s", user="u", tools=["replan", "no_action"], registry=reg, read=read, terminal=TERMINAL)
    assert res.call["name"] == "replan" and res.calls == 1


def test_new_look_ups_robot_task_and_eta():
    o, read, reg = parts()
    assert read.run("get_robot", {"robot_id": "R1"})["cluster"] == "east-wing"
    assert "error" in read.run("get_robot", {"robot_id": "nope"})
    t = new_task(1, "Deliver a wheelchair to 2B-3, routine."); out = o.handle_request(t, t["request_text"])
    t.update(spec=out.spec, lease=out.lease, status="leased")
    o.load_task = lambda tid: t if tid == t["task_id"] else None
    read = ReadTools(topo=o.topo, fleet=o.fleet, policies=o.policies, mapstate=o.mapstate, clock=o.clock, load_task=o.load_task, repo=o.repo)
    assert read.run("get_task", {"task_id": t["task_id"]})["lease"]["cluster"] == "east-wing"
    assert read.run("get_robot", {"robot_id": "R1"})["task"] == out.lease["lease_id"]
    e = read.run("eta", {"src": "E1", "dst": "2B", "speed": 0.5})
    assert e["seconds"] == round(e["length"] / 0.5)


def test_react_prompt_carries_the_options_the_code_computed():
    seen = []
    o = make_orch(override=lambda tools, kw: (seen.append(kw["messages"][0]["text"]), None)[1] if "replan" in tools else None)
    t = new_task(1, "Deliver a wheelchair to 2B-3, routine."); out = o.handle_request(t, t["request_text"])
    t.update(spec=out.spec, lease=out.lease, status="leased", request_text=t["request_text"])
    o.react.on_event(t, {"what_happened": "E3 blocked", "where": "E3", "robots_saw": ["cart"]})
    assert "OPTIONS (already computed by code)" in seen[0] and "replan_in_cluster" in seen[0] and "'feasible': True" in seen[0]


def test_model_can_escalate_to_the_operator_and_the_task_is_left_alone():
    o = make_orch(override=lambda tools, kw: tool("escalate_operator", text="Cart in E3 needs a porter.") if "replan" in tools else None)
    t = new_task(1, "Deliver a wheelchair to 2B-3, routine."); out = o.handle_request(t, t["request_text"])
    t.update(spec=out.spec, lease=out.lease, status="leased", request_text=t["request_text"])
    r = o.react.on_event(t, {"what_happened": "E3 blocked", "where": "E3", "robots_saw": ["cart"]})
    assert r.outcome is None and any("porter" in n["text"] for n in o.notifier.for_audience("operator"))


def test_wait_and_retry_looks_again_later_and_does_nothing_if_the_way_has_opened():
    o = make_orch(override=lambda tools, kw: tool("wait_and_retry", seconds=60, rationale="a cart usually moves") if "replan" in tools else None)
    t = new_task(1, "Deliver a wheelchair to 2B-3, routine."); out = o.handle_request(t, t["request_text"])
    t.update(spec=out.spec, lease=out.lease, status="leased", request_text=t["request_text"])
    r = o.react.on_event(t, {"what_happened": "E3 blocked", "where": "E3", "robots_saw": ["cart"], "segments": [{"segment_id": "E3", "state": "blocked"}]})
    assert r.outcome is None and t["task_id"] in o.waits
    assert o.ticker.tick([t]).apply == []                   # not due yet
    o.mapstate.clear("E3"); o.clock.advance(61)             # the cart moved
    res = o.ticker.tick([t])
    assert any(x["output"].get("rationale") == "the blockage cleared while waiting" for _, oc in res.apply for x in oc.rows)
    assert o.repo.current(t["task_id"])["version"] == 1     # no needless v2
