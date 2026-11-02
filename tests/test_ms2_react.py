"""MS2 Task 3: reacting to UNEXPECTED_EVENT and CLARIFY (FR-9, FR-16)."""

from cloud_helpers import LLMError, make_orch, new_task, tool
from conftest import http_json
import contracts


def leased(orch, text="Deliver a wheelchair to 2B-3, routine.", n=1):
    t = new_task(n, text)
    out = orch.handle_request(t, text)
    assert out.kind == "leased", out.reason
    t.update(spec=out.spec, lease=out.lease, reservation=out.reservation, status="leased")
    return t, out


def react_row(r):
    return next(x for x in r.rows if x["output"].get("decision") == "react")


def event(where="corridor E3", segments=None):
    p = {"what_happened": "cart blocks the corridor", "where": where, "robots_saw": ["cart"]}
    if segments:
        p["segments"] = segments
    return p


def test_blocked_corridor_replans_v2_and_records_the_closure():
    orch = make_orch()
    task, first = leased(orch)
    r = orch.react.on_event(task, event())
    assert r.outcome.kind == "leased" and r.outcome.lease["version"] == 2
    assert "E3" not in r.outcome.lease["task_sequence"][2]["params"]["via"]
    assert "E3" in orch.mapstate.closed(orch.clock.now())
    assert react_row(r)["output"]["decision"] == "react" and react_row(r)["output"]["action"] == "replan"


def test_model_with_lookups_is_used_and_its_trace_is_logged():
    calls = iter([tool("get_route", src="E1", dst="2B", avoid=["E3"]), tool("replan", avoid_zones=["E3"], rationale="E4 works")])
    orch = make_orch(override=lambda tools, kw: next(calls) if "replan" in tools else None)
    task, _ = leased(orch)
    r = orch.react.on_event(task, event())
    assert r.llm_calls == 2 and react_row(r)["who"] == "cloud" and react_row(r)["output"]["trace"][0]["tool"] == "get_route"
    assert r.outcome.lease["version"] == 2


def test_replan_starts_from_the_robots_current_zone():
    orch = make_orch()
    task, _ = leased(orch)
    orch.fleet.heartbeat("east-wing", {"robots": [{"id": "R1", "state": "busy", "zone": "E2"}]})
    r = orch.react.on_event(task, event())
    zones = r.outcome.lease["allowed_zones"]
    assert "E2" in zones and "E3" not in zones


def test_robot_parked_inside_the_blocked_zone_can_still_be_led_out():
    orch = make_orch()
    task, _ = leased(orch)
    orch.fleet.heartbeat("east-wing", {"robots": [{"id": "R1", "state": "busy", "zone": "E3"}]})
    r = orch.react.on_event(task, event())
    assert r.outcome.kind == "leased" and "E3" in r.outcome.lease["allowed_zones"]   # only so it can leave; not routed through


def test_repeat_of_an_event_the_lease_already_avoids_does_not_replan_again():
    orch = make_orch()
    task, _ = leased(orch)
    first = orch.react.on_event(task, event())
    task["lease"] = first.outcome.lease
    again = orch.react.on_event(task, event())
    assert again.outcome is None and react_row(again)["output"]["action"] == "no_action"
    assert orch.repo.current(task["task_id"])["version"] == 2


def test_model_naming_an_unknown_zone_is_corrected_then_rules_take_over_if_it_persists():
    orch = make_orch(override=lambda tools, kw: tool("replan", avoid_zones=["Narnia"], rationale="x") if "replan" in tools else None)
    task, _ = leased(orch)
    r = orch.react.on_event(task, event())
    assert react_row(r)["who"] == "rule" and "model_error" in react_row(r)["output"] and r.outcome.lease["version"] == 2


def test_model_down_uses_the_rules():
    def down(tools, kw):
        if "replan" in tools:
            raise LLMError("down")
    orch = make_orch(override=down)
    task, _ = leased(orch)
    r = orch.react.on_event(task, event())
    assert react_row(r)["who"] == "rule" and r.outcome.kind == "leased"


def test_c3_never_calls_a_model():
    orch = make_orch(mode="C3")
    task, _ = leased(orch)
    r = orch.react.on_event(task, event())
    assert r.llm_calls == 0 and orch.llm.calls == [] or all("replan" not in c for c in orch.llm.calls)
    assert r.outcome.lease["version"] == 2


def test_no_route_left_alerts_the_operator():
    orch = make_orch()
    task, _ = leased(orch)
    r = orch.react.on_event(task, event(segments=[{"segment_id": "E3", "state": "blocked"}, {"segment_id": "E4", "state": "blocked"}]))
    assert r.outcome.kind == "replan_failed"
    assert any(x["output"].get("decision") == "alert_operator" for x in r.rows)
    assert orch.notifier.for_audience("operator")


def test_clarify_the_cloud_can_answer_is_relayed_to_the_cluster():
    orch = make_orch(override=lambda tools, kw: tool("answer_clarification", answer="Use the standard pump", rationale="SOP") if "ask_requester" in tools else None)
    task, _ = leased(orch)
    r = orch.react.on_clarify(task, {"question": "Which pump?", "possible_readings": ["a", "b"]}, 1)
    assert r.answer == "Use the standard pump" and r.publish[0][0] == "nimbus/east-wing/notice"
    assert r.publish[0][1]["type"] == "ANSWER"


def test_clarify_the_cloud_cannot_answer_goes_to_the_requester():
    orch = make_orch()
    task, _ = leased(orch)
    r = orch.react.on_clarify(task, {"question": "Which pump?", "possible_readings": ["a", "b"]}, 1)
    assert r.answer is None and r.question == {"question": "Which pump?", "possible_readings": ["a", "b"]}


# -- over HTTP ---------------------------------------------------------------------------------------------------------

def _leased(api, suffix):
    _, b = http_json("POST", api + "/requests", {"text": "Deliver a wheelchair to 2B-3, routine.", "message_id": f"m-{suffix}"})
    return b["task_id"]


def esc(task_id, kind, payload, mid, version=1):
    return {"type": kind, "message_id": mid, "lease_version": version, "task_id": task_id, "payload": payload}


def test_unexpected_event_over_http_makes_v2_and_stale_v1_is_refused(api):
    tid = _leased(api, "ue1")
    st, body = http_json("POST", api + "/escalations", esc(tid, "UNEXPECTED_EVENT", event(), "e-1"))
    assert st == 201 and body["replanned"] is True and body["lease_version"] == 2
    st, body = http_json("POST", api + "/escalations", esc(tid, "UNEXPECTED_EVENT", event(), "e-2", version=1))
    assert st == 409 and body["current"] == 2
    _, tl = http_json("GET", api + f"/tasks/{tid}/timeline")
    assert any(r["output"].get("decision") == "react" for r in tl["timeline"])
    assert all(contracts.validate_document("decision_log_entry", r) == [] for r in tl["timeline"])


def test_edge_clarify_goes_to_requester_and_the_answer_is_relayed(api):
    tid = _leased(api, "cl1")
    st, body = http_json("POST", api + "/escalations", esc(tid, "CLARIFY", {"question": "Which door?", "possible_readings": ["A", "B"]}, "c-1"))
    assert st == 201 and body["status"] == "needs_clarification"
    st, body = http_json("POST", api + f"/requests/{tid}/answer", {"text": "Door A", "message_id": "a-1"})
    assert st == 201 and body["status"] == "leased" and body["relayed_to"] == "east-wing"
    _, out = http_json("GET", api + "/outbox")
    assert any(i["topic"] == "nimbus/east-wing/notice" and i["payload"]["text"] == "Door A" for i in out["outbox"])


def test_a_commit_error_other_than_conflict_does_not_leak_the_reserved_item():
    import pytest
    from cloud.leases import MemoryLeaseRepo

    class Flaky(MemoryLeaseRepo):
        def commit(self, *a, **k):
            raise RuntimeError("dynamodb throttled")

    orch = make_orch(repo=Flaky())
    before = orch.fleet.clusters["east-wing"]["stock"]["wheelchair"]
    with pytest.raises(RuntimeError):
        orch.handle_request(new_task(), "Deliver a wheelchair to 2B-3, routine.")
    assert orch.fleet.clusters["east-wing"]["stock"]["wheelchair"] == before
