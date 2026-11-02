"""P3: the C2 relay decision, and regressions for the review findings
(renew ownership, replan conflicts, unique renewal ids, stock digest, terminal states)."""

import pytest

from cloud.leases import Conflict, MemoryLeaseRepo
from cloud.orchestrator import StaleVersion
from cloud_helpers import LLMError, make_orch, new_task, tool
from conftest import http_json


def leased(orch, text="Deliver a wheelchair to 2B-3, routine.", n=1):
    task = new_task(n, text)
    out = orch.handle_request(task, text)
    assert out.kind == "leased", out.reason
    task.update(spec=out.spec, lease=out.lease, reservation=out.reservation, status="leased")
    return task, out


def event(**kw):
    return {"step_index": 2, "cmd": "go_to", "zone": "E1", "target": "2B", "situation": "ready", "attempt": 0, **kw}


# -- C2: every non-reflex step is a cloud round trip ----------------------------------------------------------------------

def test_c2_proceeds_with_one_model_call_per_step():
    orch = make_orch(mode="C2")
    task, out = leased(orch)
    before = len(orch.llm.calls)
    res = orch.decide_step(task["task_id"], 1, out.lease["cluster_id"], event())
    assert res["decision"]["action"] == "proceed" and res["decision"]["source"] == "llm"
    assert res["calls"] == 1 and len(orch.llm.calls) == before + 1
    assert res["rows"][0]["who"] == "cloud" and res["rows"][0]["output"]["decision"] == "step_proceed"


def test_c2_reroute_stays_inside_the_lease_and_avoids_blocked_zones():
    orch = make_orch(mode="C2")
    task, out = leased(orch)
    res = orch.decide_step(task["task_id"], 1, out.lease["cluster_id"],
                           event(zone=out.lease["allowed_zones"][0], blocked_zones=["E3"]))
    d = res["decision"]
    if d["action"] == "reroute":
        assert "E3" not in d["via_zones"] and set(d["via_zones"]) <= set(out.lease["allowed_zones"])
    else:
        assert d["action"] in ("wait", "abort")


def test_c2_model_answer_outside_the_lease_is_replaced_by_the_rules():
    def outside(tools, kw):
        if "decide_step" in tools:
            return tool("decide_step", action="reroute", via_zones=["ZZ-not-in-lease"], rationale="shortcut")
    orch = make_orch(mode="C2", override=outside)
    task, out = leased(orch)
    res = orch.decide_step(task["task_id"], 1, out.lease["cluster_id"], event(blocked_zones=["E3"]))
    assert res["decision"]["source"] == "rules" and res["decision"]["action"] == "wait"   # retry first
    assert "outside the lease" in res["rows"][0]["output"]["rejected_model_answer"]
    assert res["rows"][0]["who"] == "rule"


def test_c2_model_down_falls_back_to_the_c3_rules():
    def down(tools, kw):
        if "decide_step" in tools:
            raise LLMError("bedrock down")
    orch = make_orch(mode="C2", override=down)
    task, out = leased(orch)
    res = orch.decide_step(task["task_id"], 1, out.lease["cluster_id"], event())
    assert res["decision"]["source"] == "rules" and res["decision"]["action"] == "proceed"


def test_c2_relay_refuses_stale_version_and_wrong_cluster():
    orch = make_orch(mode="C2")
    task, out = leased(orch)
    orch.replan(task, blocked_zones=["E3"], reason="x")
    with pytest.raises(StaleVersion):
        orch.decide_step(task["task_id"], 1, out.lease["cluster_id"], event())
    assert orch.decide_step(task["task_id"], 2, "west-wing", event())["status"] == 409


def test_c1_uses_fewer_model_calls_per_task_than_c2():
    """NFR-4 shape: C2 pays a round trip for each of the lease's steps, C1 does not."""
    c1, c2 = make_orch(mode="C1"), make_orch(mode="C2")
    t1, o1 = leased(c1)
    t2, o2 = leased(c2)
    for i in range(len(o2.lease["task_sequence"])):
        c2.decide_step(t2["task_id"], 1, o2.lease["cluster_id"], event(step_index=i))
    assert len(c1.llm.calls) * 2 <= len(c2.llm.calls)


def test_relay_endpoint_is_c2_only(api):
    status, body = http_json("POST", api + "/relay/decide",
                             {"cluster_id": "east-wing", "lease_id": "lease-task-000001-v1", "lease_version": 1,
                              "step_index": 0, "message_id": "m-relay"})
    assert status == 409 and "only in C2" in body["error"]


# -- review findings -----------------------------------------------------------------------------------------------------

def test_memory_repo_renew_refuses_when_the_robot_now_belongs_to_another_task():
    orch = make_orch()
    t1, o1 = leased(orch, n=1)
    robot = o1.lease["assigned_robots"][0]
    orch.repo.claims[f"robot:{robot}"]["task_id"] = "task-000099"      # taken over after expiry
    with pytest.raises(Conflict):
        orch.repo.renew(t1["task_id"], 1, "2099-01-01T00:00:00Z", now=orch.clock.now())


def test_replan_reports_failure_instead_of_raising_when_the_commit_conflicts():
    class Racy(MemoryLeaseRepo):
        def commit(self, lease, claim_keys, *, now, stock=None):
            if lease["version"] > 1:
                raise Conflict(["version moved"])
            super().commit(lease, claim_keys, now=now, stock=stock)

    orch = make_orch(repo=Racy())
    task, _ = leased(orch)
    out = orch.replan(task, blocked_zones=["E3"], reason="cart")
    assert out.kind == "replan_failed" and "version moved" in out.reason


def test_renewal_message_id_follows_the_new_expiry_not_the_wall_clock_second():
    published = []
    orch = make_orch(published=published)
    task, _ = leased(orch)
    orch.clock.advance(3660)
    first = orch.renew(task["task_id"], 1)
    orch.clock.advance(100)
    second = orch.renew(task["task_id"], 1)
    ids = [p["message_id"] for t, p in published if t.endswith("/renewal")]
    assert ids == [f"renew-{first['lease_id']}-{first['expiry_ts']}", f"renew-{second['lease_id']}-{second['expiry_ts']}"]
    assert len(set(ids)) == 2


def test_edge_stock_digest_cannot_hand_back_an_item_that_is_leased_out():
    orch = make_orch()
    before = orch.fleet.clusters["east-wing"]["stock"]["monitor"]
    task, out = leased(orch, "Deliver a monitor to 2A", n=1)
    assert orch.fleet.clusters["east-wing"]["stock"]["monitor"] == before - 1
    orch.fleet.heartbeat("east-wing", {"stock": {"monitor": before}})      # the edge's shelves still show it
    assert orch.fleet.clusters["east-wing"]["stock"]["monitor"] == before - 1
    orch.finish(task)                                                     # delivered: now it really is gone
    orch.fleet.heartbeat("east-wing", {"stock": {"monitor": before - 1}})
    assert orch.fleet.clusters["east-wing"]["stock"]["monitor"] == before - 1


def test_cancel_after_finish_does_not_return_a_consumed_item():
    orch = make_orch()
    before = orch.fleet.clusters["east-wing"]["stock"]["monitor"]
    task, _ = leased(orch, "Deliver a monitor to 2A")
    orch.finish(task)
    orch.cancel(task)
    assert orch.fleet.clusters["east-wing"]["stock"]["monitor"] == before - 1


# -- over HTTP: terminal states are final ----------------------------------------------------------------------------------

def _leased_over_http(api):
    _, body = http_json("POST", api + "/requests", {"text": "Deliver a wheelchair to ward 3, routine.", "message_id": "m-t1"})
    return body["task_id"], body["lease"] if "lease" in body else None


def test_cancel_or_pause_on_a_completed_task_is_refused_and_late_output_cannot_revive_a_cancelled_one(api):
    done, _ = _leased_over_http(api)
    ready = {"type": "OUTPUT_READY", "message_id": "m-or-1", "lease_version": 1, "task_id": done,
             "payload": {"result_ref": "s3://nimbus-outputs/a.jpg"}}
    assert http_json("POST", api + "/escalations", ready)[0] == 201
    status, body = http_json("POST", api + f"/tasks/{done}/control", {"action": "cancel", "message_id": "m-c-1"})
    assert status == 409 and body["status"] == "completed"
    assert http_json("GET", api + f"/tasks/{done}")[1]["status"] == "completed"

    _, b = http_json("POST", api + "/requests", {"text": "Deliver a wheelchair to ward 3, routine.", "message_id": "m-t2"})
    other = b["task_id"]
    assert http_json("POST", api + f"/tasks/{other}/control", {"action": "cancel", "message_id": "m-c-2"})[0] == 201
    late = dict(ready, task_id=other, message_id="m-or-2")
    http_json("POST", api + "/escalations", late)
    assert http_json("GET", api + f"/tasks/{other}")[1]["status"] == "cancelled"


def test_c2_still_understands_and_allocates_with_the_model():
    """C2 differs from C1 at the edge only; the cloud's own two model calls remain."""
    orch = make_orch(mode="C2")
    leased(orch)
    assert len(orch.llm.calls) == 2
