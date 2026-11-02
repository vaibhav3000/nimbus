"""Efficiency: no model call when there is nothing to choose, none for an idle queue, and gated retries."""

from cloud.pack import Pack
from cloud_helpers import make_orch, new_task

SIM = Pack.load("hospital_sim")


def req(orch, n, text="Deliver a blanket to ward 3, routine"):
    t = new_task(n, text)
    out = orch.handle_request(t, text)
    t.update(spec=out.spec, status=out.kind, lease=out.lease, reservation=out.reservation, request_text=text)
    return t, out


def test_a_normal_request_costs_one_model_call_when_one_cluster_can_take_it():
    orch = make_orch()                                      # hand-made map: only east covers 2B-3
    req(orch, 1, "Deliver a wheelchair to 2B-3, routine.")
    assert len(orch.llm.calls) == 1


def test_the_model_is_asked_only_when_two_clusters_could_take_the_task_and_sees_the_ranked_candidates():
    seen = []
    orch = make_orch(pack=SIM, override=lambda tools, kw: seen.append(kw["messages"][-1]["text"]) if "propose_allocation" in tools else None)
    req(orch, 1)
    assert len(orch.llm.calls) == 2 and "CANDIDATES (nearest first)" in seen[0] and "east-wing" in seen[0] and "west-wing" in seen[0]


def test_nobody_eligible_means_no_model_call_at_all():
    orch = make_orch(pack=SIM)
    for i in range(1, 3):
        req(orch, i)                                        # both robots busy
    before = len(orch.llm.calls)
    t, out = req(orch, 3)
    assert out.kind == "queued" and len(orch.llm.calls) - before == 1      # understanding only, no allocation attempts


def test_idle_drains_cost_nothing_and_a_freed_robot_retries_the_queue_once():
    orch = make_orch(pack=SIM)
    tasks = dict(((t["task_id"], t) for t, _ in (req(orch, i) for i in range(1, 6))))
    assert sum(t["status"] == "queued" for t in tasks.values()) == 3
    before = len(orch.llm.calls)
    for _ in range(10):
        orch.drain(tasks.get)
    assert len(orch.llm.calls) == before                     # nothing changed: no model call, no retry work
    first = next(t for t in tasks.values() if t["status"] == "leased")
    orch.finish(first); first["status"] = "completed"
    done = orch.drain(tasks.get)
    assert len(done) == 1 and done[0][1].kind == "leased"
    assert len(orch.llm.calls) - before <= 2                 # at most the one real decision, not one per queued task


def test_epoch_moves_on_robot_stock_health_restriction_and_closure_changes():
    orch = make_orch()
    e = orch.epoch()
    orch.fleet.heartbeat("east-wing", {"robots": [{"id": "R1", "state": "failed"}]}); e2 = orch.epoch(); assert e2 > e
    orch.fleet.set_stock("east-wing", {"monitor": 9}); e3 = orch.epoch(); assert e3 > e2
    orch.policies.declare("2A", "isolation"); e4 = orch.epoch(); assert e4 > e3
    orch.mapstate.report("E3", "blocked", "x", orch.clock.now()); e5 = orch.epoch(); assert e5 > e4
    orch.clock.advance(1000); orch.mapstate.closed(orch.clock.now()); assert orch.epoch() > e5
