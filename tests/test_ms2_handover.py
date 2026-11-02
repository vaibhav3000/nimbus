"""MS2 Task 4: hand-over between clusters (SC-2, FR-16, T5)."""

import contracts
from cloud.pack import Pack
from cloud_helpers import make_orch, new_task
from conftest import http_json

CROSS = "Move a monitor from 2A to 2C, routine"          # east -> west: no single cluster covers both ends


def start(orch, text=CROSS, n=1):
    t = new_task(n, text)
    out = orch.handle_request(t, text)
    t.update(spec=out.spec, lease=out.lease, reservation=out.reservation, status=out.kind, request_text=text,
             legs=out.legs, leg_index=out.leg_index or 0)
    return t, out


def event(zone, state="closed"):
    return {"what_happened": f"{zone} is {state}", "where": zone, "robots_saw": ["door shut"],
            "segments": [{"segment_id": zone, "state": state}]}


def test_request_across_wings_gets_two_legs_and_a_leg_1_lease():
    orch = make_orch()
    t, out = start(orch)
    assert out.kind == "leased" and len(out.legs) == 2 and out.leg_index == 0
    hand_off = out.legs[0]["destination"]
    assert hand_off in ("W0", "W1") and out.legs[1]["source"] == hand_off and out.legs[1]["destination"] == "2C"
    assert out.lease["cluster_id"] == "east-wing" and out.lease["task_sequence"][2]["params"]["to"] == hand_off
    assert contracts.validate_document("lease", out.lease) == []
    assert out.rows[0]["output"]["decision"] == "plan_handover"


def test_leg_2_is_the_next_version_for_the_other_cluster_and_the_robots_swap_in_one_commit():
    orch = make_orch()
    t, first = start(orch)
    r_a = first.lease["assigned_robots"][0]
    nxt = orch.advance_leg(t)
    assert nxt.kind == "leased" and nxt.lease["version"] == 2 and nxt.lease["cluster_id"] == "west-wing"
    assert nxt.lease["task_sequence"][0]["params"]["to"] == first.legs[0]["destination"]      # picks up at the hand-off
    assert nxt.lease["task_sequence"][2]["params"]["to"] == "2C"
    now = orch.clock.now()
    assert orch.repo.holder(f"robot:{r_a}", now) is None and orch.repo.holder(f"robot:{nxt.lease['assigned_robots'][0]}", now)
    assert not orch.has_next_leg({**t, "leg_index": 1})


def test_leg_2_queues_when_the_second_cluster_is_busy_and_leg_1_is_not_abandoned():
    orch = make_orch()
    t, first = start(orch)
    for i, rid in enumerate(("R4", "R5", "R6")):                    # every west robot is busy with other work
        other = new_task(10 + i, "x")
        out = orch.handle_request(other, "Deliver a blanket to ward 5, routine.")
        assert out.kind == "leased"
    nxt = orch.advance_leg(t)
    assert nxt.kind == "queued" and nxt.leg_index == 1
    assert orch.repo.current(t["task_id"])["version"] == 1          # leg 1's lease is still the live one
    t.update(status="queued", leg_index=1, spec=t["spec"])
    held = new_task(10, "x")
    held.update(reservation=None)
    orch.finish(held)
    done = orch.drain(lambda tid: t if tid == t["task_id"] else None)
    assert [x[0]["task_id"] for x in done] == [t["task_id"]] and done[0][1].lease["version"] == 2


def test_gate_closes_midway_and_the_last_leg_is_replanned_under_a_new_version():
    orch = make_orch()
    t, first = start(orch)
    gate = first.legs[0]["destination"]
    other_gate = "W1" if gate == "W0" else "W0"
    orch.fleet.heartbeat("east-wing", {"robots": [{"id": first.lease["assigned_robots"][0], "state": "busy", "zone": "E4"}]})
    r = orch.react.on_event(t, event(gate))
    assert r.outcome.kind == "leased" and r.outcome.lease["version"] == 2
    assert r.outcome.legs[0]["destination"] == other_gate and r.outcome.lease["forbidden_zones"] == [gate]
    assert any(x["output"].get("decision") == "reassign" for x in r.rows + r.outcome.rows)


def test_both_gates_closed_gives_a_reason_and_alerts_the_operator_instead_of_looping():
    orch = make_orch()
    t, first = start(orch)
    orch.fleet.heartbeat("east-wing", {"robots": [{"id": first.lease["assigned_robots"][0], "state": "busy", "zone": "E4"}]})
    orch.mapstate.report("W1" if first.legs[0]["destination"] == "W0" else "W0", "closed", "edge", orch.clock.now())
    r = orch.react.on_event(t, event(first.legs[0]["destination"]))
    assert r.outcome.kind == "replan_failed" and r.outcome.reason
    assert any(x["output"].get("decision") == "alert_operator" for x in r.rows)
    assert orch.repo.current(t["task_id"])["version"] == 1


def test_robot_position_unknown_cannot_be_reassigned():
    orch = make_orch()
    t, first = start(orch)
    orch.fleet.clusters["east-wing"]["robots"]["R1"]["zone"] = "somewhere-not-on-the-map"
    out = orch.reassign(t, "gate shut")
    assert out.kind == "replan_failed" and "unknown" in out.reason


def test_another_cluster_collects_the_item_where_the_first_robot_is_parked_on_the_gazebo_map():
    orch = make_orch(pack=Pack.load("hospital_sim"))
    t = new_task(1, "x")
    text = "Deliver a wheelchair to ward 3, routine."
    out = orch.handle_request(t, text)
    assert out.kind == "leased"
    t.update(spec=out.spec, lease=out.lease, reservation=out.reservation, status="leased", request_text=text)
    old = out.lease["cluster_id"]
    wing = "east" if old == "east-wing" else "west"
    robot = out.lease["assigned_robots"][0]
    orch.fleet.heartbeat(old, {"robots": [{"id": robot, "state": "busy", "zone": "nurse_station_approach"}]})
    r = orch.react.on_event(t, event(f"{wing}_south_hall"))
    assert r.outcome.kind == "leased" and r.outcome.lease["version"] == 2 and r.outcome.lease["cluster_id"] != old
    stop = [p for topic, p in r.outcome.publish if topic == f"nimbus/{old}/lease"][0]
    assert stop["version"] == 2 and stop["task_sequence"][0]["cmd"] == "stop"
    assert any(n["cluster_id"] == old and n["type"] == "WARNING" for n in orch.notifier.for_audience("cluster"))
    assert contracts.validate_document("lease", r.outcome.lease) == []


def test_t5_cross_wing_hand_over_end_to_end(api):
    st, b = http_json("POST", api + "/requests", {"text": CROSS, "message_id": "m-ho1"})
    tid = b["task_id"]
    assert st == 201 and b["status"] == "leased"
    assert http_json("GET", api + f"/tasks/{tid}")[1]["lease"]["cluster_id"] == "east-wing"
    done = {"type": "OUTPUT_READY", "message_id": "o-1", "lease_version": 1, "task_id": tid, "payload": {"result_ref": "s3://x/leg1"}}
    st, body = http_json("POST", api + "/escalations", done)
    assert st == 201 and body["status"] == "leased"                         # not completed: there is a second leg
    _, task = http_json("GET", api + f"/tasks/{tid}")
    assert task["lease"]["version"] == 2 and task["lease"]["cluster_id"] == "west-wing" and task["leg_index"] == 1
    done2 = dict(done, message_id="o-2", lease_version=2, payload={"result_ref": "s3://x/leg2"})
    assert http_json("POST", api + "/escalations", done2)[0] == 201
    assert http_json("GET", api + f"/tasks/{tid}")[1]["status"] == "completed"
