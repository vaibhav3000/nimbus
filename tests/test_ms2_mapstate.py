"""MS2 Task 1: the shared map. A zone the edge reported shut stays shut for later requests."""

from datetime import timedelta

from cloud_helpers import make_orch, new_task
from cloud.pack import Pack
from cloud.mapstate import MapState
from cloud.topology import Topology


def leased(orch, text, n=1):
    t = new_task(n, text)
    out = orch.handle_request(t, text)
    t.update(spec=out.spec, lease=out.lease, reservation=out.reservation, status=out.kind)
    return t, out


def test_closed_corridor_is_avoided_by_a_later_request():
    orch = make_orch()
    orch.mapstate.report("E3", "blocked", "east-wing", orch.clock.now())
    _, out = leased(orch, "Deliver a wheelchair to 2B-3, routine.")
    assert out.kind == "leased" and "E3" not in out.lease["allowed_zones"] and "E4" in out.lease["allowed_zones"]


def test_closure_binds_urgent_requests_too():
    orch = make_orch()
    orch.mapstate.report("2A", "closed", "east-wing", orch.clock.now())
    _, out = leased(orch, "Deliver a monitor to 2A, urgent")
    assert out.kind == "queued" or out.kind == "rejected"      # never leased through a shut zone


def test_closure_expires_after_ttl():
    orch = make_orch()
    orch.mapstate.report("E3", "blocked", "east-wing", orch.clock.now(), ttl_s=60)
    assert "E3" in orch.mapstate.closed(orch.clock.now())
    orch.clock.advance(61)
    assert orch.mapstate.closed(orch.clock.now()) == {}


def test_unknown_zone_or_state_is_ignored():
    m = MapState(Topology(Pack.load("hospital")))
    now = make_orch().clock.now()
    assert m.report("nowhere", "blocked", "x", now) is None and m.report("E3", "fine", "x", now) is None


def test_affected_clusters_are_those_covering_or_routed_through_the_zone():
    orch = make_orch()
    assert orch.mapstate.affected_clusters("E3", orch.repo, orch.clock.now()) == ["east-wing"]
    assert orch.mapstate.affected_clusters("W0", orch.repo, orch.clock.now()) == ["east-wing", "west-wing"]
