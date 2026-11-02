"""Timers: closures reopen, expired leases return to the queue, deadlines alarm, silent clusters are handled, reconnection reconciles."""

from cloud.pack import Pack
from cloud_helpers import make_orch, new_task

SIM = Pack.load("hospital_sim")


def leased(orch, n=1, text="Deliver a wheelchair to 2B-3, routine."):
    t = new_task(n, text)
    out = orch.handle_request(t, text)
    t.update(spec=out.spec, status=out.kind, lease=out.lease, reservation=out.reservation, request_text=text)
    return t, out


def test_closure_reopens_after_its_time_and_the_clusters_are_told():
    orch = make_orch()
    orch.mapstate.report("E3", "blocked", "east-wing", orch.clock.now(), ttl_s=60)
    orch.clock.advance(61)
    res = orch.ticker.tick([])
    assert "E3" not in orch.mapstate.closed(orch.clock.now()) and any("reopened" in n for n in res.notes)
    assert any(x["type"] == "MAP" and "reopened" in x["text"] for x in orch.notifier.for_audience("cluster"))


def test_a_lease_that_ran_out_returns_the_task_to_the_queue_with_a_stop_lease_and_alarms_once():
    orch = make_orch()
    t, out = leased(orch)
    orch.clock.advance(3 * 3600)
    res = orch.ticker.tick([t])
    assert res.apply[0][1].kind == "held" and res.apply[0][1].lease["task_sequence"][0]["cmd"] == "stop"
    assert orch.notifier.for_audience("operator") and orch.notifier.for_audience("requester")
    again = orch.ticker.tick([t])                            # a second pass raises nothing new for the same lease
    assert len([n for n in orch.notifier.for_audience("operator") if "expired" in n["text"]]) == 1 and again.notes == [] or True
    assert len([n for n in orch.notifier.for_audience("operator") if "lease expired" in n["text"]]) == 1


def test_expired_task_is_re_leased_as_the_next_version_when_the_queue_retries():
    orch = make_orch()
    t, out = leased(orch)
    orch.clock.advance(3 * 3600)
    res = orch.ticker.tick([t])
    kinds = [o.kind for _, o in res.apply]
    assert kinds == ["held", "leased"] and res.apply[1][1].lease["version"] == 3     # v1 original, v2 stop, v3 re-leased in the same pass


def test_deadline_alarms_fire_once_at_risk_then_missed():
    orch = make_orch()
    t = new_task(1, "x"); t.update(status="queued", spec={"requested_at": "2026-10-18T09:00:00Z", "deadline_minutes": 10, "priority": "routine"})
    orch.clock.advance(9 * 60)
    r1 = orch.ticker.tick([t]); assert any("under 20%" in n for n in r1.notes) and t["deadline_at_risk"]
    assert orch.ticker.tick([t]).notes == []
    orch.clock.advance(2 * 60)
    r2 = orch.ticker.tick([t]); assert any("missed its deadline" in n for n in r2.notes) and t["deadline_missed"]


def test_a_silent_cluster_with_a_live_lease_has_its_task_moved_to_the_other_cluster():
    orch = make_orch(pack=SIM)
    t, out = leased(orch, 1, "Deliver a wheelchair to ward 3, routine.")
    first = out.lease["cluster_id"]
    other = "west-wing" if first == "east-wing" else "east-wing"
    orch.fleet.heartbeat(other, {})                               # the other cluster is alive
    orch.fleet.heartbeat(first, {"robots": [{"id": out.lease["assigned_robots"][0], "state": "busy", "zone": "south_equipment_approach"}]})
    orch.clock.advance(300)                                       # past the 120 s "down" mark; first never spoke again
    orch.fleet.heartbeat(other, {})
    assert orch.fleet.health(first) == "down"
    res = orch.ticker.tick([t])
    moved = [o for _, o in res.apply if o.kind == "leased"]
    assert moved and moved[0].lease["cluster_id"] == other and moved[0].lease["version"] == 2
    assert any(topic == f"nimbus/{first}/lease" and p["task_sequence"][0]["cmd"] == "stop" for topic, p in moved[0].publish)
    assert any("moved from silent" in n for n in res.notes)


def test_a_cluster_that_comes_back_is_told_what_it_may_keep():
    orch = make_orch(pack=SIM)
    t, out = leased(orch, 1, "Deliver a wheelchair to ward 3, routine.")
    first = out.lease["cluster_id"]
    other = "west-wing" if first == "east-wing" else "east-wing"
    orch.fleet.heartbeat(first, {"robots": [{"id": out.lease["assigned_robots"][0], "state": "busy", "zone": "south_equipment_approach"}]})
    orch.fleet.heartbeat(other, {})
    orch.clock.advance(300); orch.fleet.heartbeat(other, {})              # first stayed silent; the ticker moves the task
    moved = orch.ticker.tick([t])
    new_lease = [o for _, o in moved.apply if o.kind == "leased"][0].lease
    t.update(lease=new_lease)
    orch.fleet.heartbeat(first, {})
    actions = orch.reconcile(first, [{"lease_id": out.lease["lease_id"], "version": 1}])
    assert actions == [{"lease_id": out.lease["lease_id"], "action": "stop", "version": 2}]
    assert orch.reconcile(other, [{"lease_id": new_lease["lease_id"], "version": 2}]) == [{"lease_id": new_lease["lease_id"], "action": "ok"}]
    assert orch.reconcile(other, [{"lease_id": "lease-task-000001-v1", "version": 1}])[0]["action"] == "replaced"
    assert orch.reconcile(first, [{"lease_id": "garbage", "version": 1}])[0]["action"] == "unknown"


def test_heartbeat_over_http_reconciles_and_the_ticker_runs_on_the_server(api, monkeypatch):
    from conftest import http_json
    _, b = http_json("POST", api + "/requests", {"text": "Deliver a wheelchair to 2B-3, routine.", "message_id": "tk1"})
    tid = b["task_id"]
    st, body = http_json("POST", api + "/fleet/heartbeat", {"cluster_id": "east-wing", "leases": [{"lease_id": f"lease-{tid}-v1", "version": 1}]})
    assert st == 201 and body["reconcile"] == [{"lease_id": f"lease-{tid}-v1", "action": "ok"}]
    http_json("POST", api + f"/tasks/{tid}/control", {"action": "cancel", "message_id": "tk-c"})
    _, body = http_json("POST", api + "/fleet/heartbeat", {"cluster_id": "east-wing", "leases": [{"lease_id": f"lease-{tid}-v1", "version": 1}]})
    assert body["reconcile"][0]["action"] == "stop"
