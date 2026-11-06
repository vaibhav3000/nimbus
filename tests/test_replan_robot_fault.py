"""A robot that fails mid-lease is replaced by another robot of the same cluster, and a robot fault does not close
the place where the robot stopped (found by experiment C6)."""

from cloud.pack import Pack
from cloud.react import blocked_zones
from cloud.topology import Topology
from cloud_helpers import make_orch, new_task

SIM6 = Pack.load("hospital_sim6")


def leased(orch, n, text):
    t = new_task(n, text)
    out = orch.handle_request(t, text)
    t.update(status=out.kind, spec=out.spec, lease=out.lease, reservation=out.reservation)
    return t, out


def test_failed_robot_is_replaced_by_a_free_robot_of_the_same_cluster():
    orch = make_orch(pack=SIM6)
    task, out = leased(orch, 1, "Deliver a wheelchair to 2B, routine.")
    assert out.kind == "leased"
    cid, robot = out.lease["cluster_id"], out.lease["assigned_robots"][0]
    orch.fleet.heartbeat(cid, {"robots": [{"id": robot, "state": "failed"}]})
    new = orch.replan(task, blocked_zones=[], reason=f"{robot} drive fault")
    assert new.kind == "leased" and new.lease["version"] == 2
    assert new.lease["cluster_id"] == cid and new.lease["assigned_robots"][0] != robot


def test_replan_keeps_the_robot_when_it_is_fine():
    orch = make_orch(pack=SIM6)
    task, out = leased(orch, 1, "Deliver a wheelchair to 2B, routine.")
    new = orch.replan(task, blocked_zones=[], reason="re-plan")
    assert new.kind == "leased" and new.lease["assigned_robots"] == out.lease["assigned_robots"]


def test_a_robot_fault_does_not_close_the_zone_it_stopped_in():
    topo = Topology(SIM6)
    fault = {"what_happened": "R3 stopped: drive motor fault", "where": "south_equipment_approach", "robots_saw": ["R3: fault"]}
    blocked = {"what_happened": "corridor blocked by a bed", "where": "east_mid_hall", "robots_saw": ["R3: blocked"]}
    assert blocked_zones(topo, fault) == []
    assert blocked_zones(topo, blocked) == ["east_mid_hall"]
    seg = {**fault, "segments": [{"segment_id": "east_mid_hall", "state": "blocked"}]}
    assert blocked_zones(topo, seg) == ["east_mid_hall"]
