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


def test_an_edge_message_is_not_held_up_by_a_request_that_is_waiting_on_the_model(tmp_path, monkeypatch):
    """With a slow model (1 s per call) a heartbeat used to wait behind the whole request (measured 1.8 s)."""
    import threading
    import time

    import server
    from cloud import llm as L
    from conftest import http_json

    orig = L.RuleBasedLLM.converse
    monkeypatch.setattr(L.RuleBasedLLM, "converse", lambda self, **kw: (time.sleep(1.0), orig(self, **kw))[1])
    httpd = server.make_server("127.0.0.1", 0, tmp_path / "d")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    api = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        t = threading.Thread(target=lambda: http_json("POST", api + "/requests", {"text": "Deliver a wheelchair to ward 3, routine", "message_id": "slow1"}))
        t.start(); time.sleep(0.25)
        t0 = time.time(); http_json("POST", api + "/fleet/heartbeat", {"cluster_id": "east-wing"}); waited = time.time() - t0
        t.join()
        assert waited < 0.5, f"heartbeat waited {waited:.2f}s behind a request"
    finally:
        httpd.shutdown()


def test_two_requests_planned_at_once_never_share_a_robot(tmp_path, monkeypatch):
    import threading
    import time

    import server
    from cloud import llm as L
    from conftest import http_json

    orig = L.RuleBasedLLM.converse
    monkeypatch.setattr(L.RuleBasedLLM, "converse", lambda self, **kw: (time.sleep(0.4), orig(self, **kw))[1])
    httpd = server.make_server("127.0.0.1", 0, tmp_path / "d")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    api = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        ids = []
        def go(i):
            ids.append(http_json("POST", api + "/requests", {"text": "Deliver a blanket to ward 3, routine", "message_id": f"cc{i}"})[1]["task_id"])
        ts = [threading.Thread(target=go, args=(i,)) for i in range(4)]
        [t.start() for t in ts]; [t.join() for t in ts]
        leases = [http_json("GET", api + f"/tasks/{i}")[1] for i in ids]
        robots = [l["lease"]["assigned_robots"][0] for l in leases if l["status"] == "leased"]
        assert len(robots) == len(set(robots)) == 3 and sum(l["status"] == "queued" for l in leases) == 1   # east has 3 robots, 4 requests
    finally:
        httpd.shutdown()
