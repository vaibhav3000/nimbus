"""Unit tests for Person 3's building blocks (topology, fleet, policy, lease
repo, scheduler, checker)."""

import random
from datetime import timedelta

import pytest

from cloud import nlp
from cloud.allocator import FleetView
from cloud.checker import Proposal, check
from cloud.clock import FixedClock, iso
from cloud.fleet import FleetRegistry
from cloud.leases import Conflict, MemoryLeaseRepo, claim_keys_for
from cloud.pack import Pack
from cloud.policy import PolicyBook
from cloud.scheduler import Scheduler
from cloud.topology import Topology

pack = Pack.load("hospital")
topo = Topology(pack)


# -- topology -------------------------------------------------------------------

def test_locate_beds_wards_and_unknowns():
    assert topo.locate("pump at 2B-12, urgent")["zone"] == "2B-12"
    assert topo.locate("blanket to ward 5")["zone"] == "ward-5"
    assert topo.locate("send it to 2c please")["zone"] == "2C"
    assert topo.locate("to ward 99") is None
    assert topo.locate("bed 2B-99") is None  # beyond the ward's 20 beds: ask, don't guess
    assert topo.locate("in 5 min") is None


def test_routes_give_a_detour_inside_the_lease():
    zones = topo.cluster_zones("east-wing")
    routes = topo.routes("E-STORE", "2B", allowed=zones)
    assert routes[0] == ["E-STORE", "E1", "E2", "E3", "2B"]
    assert routes[1] == ["E-STORE", "E1", "E2", "E4", "2B"]  # SC-1: E3 blocked, E4 used


def test_forbidden_zone_is_avoided_or_unreachable():
    zones = topo.cluster_zones("east-wing")
    assert topo.shortest_path("E-STORE", "2B", allowed=zones, avoid={"E3"}) == ["E-STORE", "E1", "E2", "E4", "2B"]
    assert topo.shortest_path("E-STORE", "2B", allowed=zones, avoid={"E3", "E4"}) is None
    assert topo.shortest_path("E-STORE", "ward-5", allowed=zones) is None  # other wing


def test_cluster_zones_include_shared_gates_only():
    east = topo.cluster_zones("east-wing")
    assert {"W0", "W1"} <= east and "W2" not in east and "2B" in east


# -- nlp -------------------------------------------------------------------------

def test_parse_request_priority_deadline_and_missing():
    p = nlp.parse(pack, topo, "Need an infusion pump at 2B-12, urgent.")
    assert (p.item, p.priority, p.deadline_minutes, p.missing) == ("infusion_pump", "urgent", 12, [])
    assert nlp.parse(pack, topo, "wheelchair to ward 3 within 20 minutes").deadline_minutes == 20
    assert nlp.parse(pack, topo, "wheelchair to ward 3 within 2 hours").deadline_minutes == 120
    assert nlp.parse(pack, topo, "STAT monitor to 2A").priority == "urgent"   # v5 has two classes
    assert nlp.parse(pack, topo, "a monitor to 2A soon").deadline_minutes == 30
    assert nlp.parse(pack, topo, "something to ward 3").missing == ["item"]
    assert nlp.parse(pack, topo, "a wheelchair").missing == ["destination"]


def test_parse_reads_from_and_to_and_defaults_the_destination():
    p = nlp.parse(pack, topo, "Move the pump from 2C to 2A")
    assert (p.source["zone"], p.destination["zone"]) == ("2C", "2A")
    out = nlp.parse(pack, topo, "Get the spare pumps out of 2C")
    assert (out.source["zone"], out.destination["zone"]) == ("2C", "W-STORE")  # back to that area's store
    assert nlp.spec_from(out)["source"] == "2C"


@pytest.mark.parametrize("answer,item,fields", [
    ("Yes, a paediatric pump please.", "infusion_pump_paediatric", {}),
    ("yes", "infusion_pump_paediatric", {}),
    ("No, standard", "infusion_pump", {"variant_confirmed_by_requester": True}),
    ("no", "infusion_pump", {"variant_confirmed_by_requester": True}),
    ("not standard, paediatric", "infusion_pump_paediatric", {}),
    ("not standard", "infusion_pump_paediatric", {}),
    ("hmm maybe", "infusion_pump", {})])
def test_answers_resolve_the_rules_question(answer, item, fields):
    p = nlp.parse(pack, topo, "Need an infusion pump at 2B-12", answer=answer)
    assert (p.item, p.fields) == (item, fields)


# -- fleet health ------------------------------------------------------------------

def test_health_transitions_with_time():
    clock = FixedClock()
    fleet = FleetRegistry.load(clock, pack)
    assert fleet.health("east-wing") == "unknown"
    assert fleet.heartbeat("east-wing", {"robots": [{"id": "R1", "state": "failed"}], "stock": {"wheelchair": 1}})
    assert fleet.health("east-wing") == "healthy"
    snap = {c["cluster_id"]: c for c in fleet.snapshot()}["east-wing"]
    assert [r["state"] for r in snap["robots"]][0] == "failed" and snap["stock"]["wheelchair"] == 1
    clock.advance(31)
    assert fleet.health("east-wing") == "stale"
    clock.advance(100)
    assert fleet.health("east-wing") == "down"
    assert fleet.heartbeat("north-wing") is False  # unknown cluster: ignored, not an error


def test_stock_reserve_and_release():
    fleet = FleetRegistry.load(FixedClock(), pack)
    n = fleet.clusters["east-wing"]["stock"]["monitor"]
    assert fleet.reserve_stock("east-wing", "monitor") and fleet.reserve_stock("east-wing", "monitor")
    assert not fleet.reserve_stock("east-wing", "monitor") and n == 2
    fleet.release_stock("east-wing", "monitor")
    assert fleet.reserve_stock("east-wing", "monitor")


# -- policy ----------------------------------------------------------------------------

def test_policy_retrieval_cites_ids_and_restrictions_are_runtime():
    pb = PolicyBook(pack)
    ids = [p["id"] for p in pb.retrieve("infusion pump to a child ward", extra_terms=("paediatric",))]
    assert ids[0] == "SOP-PAED-01"
    assert pb.restrictions() == {}
    pb.declare("2C", "isolation")
    r = pb.restrictions()["2C"]
    assert (r["policy"], r["effect"], r["exempt_priorities"]) == ("SOP-ISO-02", "hold", ["urgent"])
    assert "SOP-ISO-02" in [p["id"] for p in pb.retrieve("anything")]
    pb.lift("2C")
    assert pb.restrictions() == {}
    with pytest.raises(KeyError):
        pb.declare("2C", "flood")   # this pack defines no such restriction kind


# -- lease repo invariants ------------------------------------------------------------------

def lease(task, version, robots, expiry, zones=("E1",)):
    return {"lease_id": f"lease-{task}-v{version}", "version": version, "expiry_ts": expiry,
            "assigned_robots": list(robots), "allowed_zones": list(zones)}


def test_one_live_holder_per_robot_and_expiry_frees_it():
    clock = FixedClock()
    repo = MemoryLeaseRepo()
    exp = iso(clock.now() + timedelta(minutes=5))
    repo.commit(lease("task-000001", 1, ["R1"], exp), ["robot:R1"], now=clock.now())
    with pytest.raises(Conflict):
        repo.commit(lease("task-000002", 1, ["R1"], exp), ["robot:R1"], now=clock.now())
    clock.advance(301)  # holder's lease lapsed: the claim frees itself
    repo.commit(lease("task-000002", 1, ["R1"], exp), ["robot:R1"], now=clock.now())


def test_versions_are_strictly_plus_one_and_old_claims_move():
    clock = FixedClock()
    repo = MemoryLeaseRepo()
    exp = iso(clock.now() + timedelta(minutes=5))
    repo.commit(lease("task-000001", 1, ["R1"], exp), ["robot:R1"], now=clock.now())
    with pytest.raises(Conflict):  # skipping a version
        repo.commit(lease("task-000001", 3, ["R1"], exp), ["robot:R1"], now=clock.now())
    repo.commit(lease("task-000001", 2, ["R2"], exp), ["robot:R2"], now=clock.now())
    assert repo.holder("robot:R1", clock.now()) is None  # v2 dropped R1
    assert repo.current("task-000001")["version"] == 2


def test_renew_never_shortens_and_rejects_stale_version():
    clock = FixedClock()
    repo = MemoryLeaseRepo()
    exp = iso(clock.now() + timedelta(minutes=5))
    repo.commit(lease("task-000001", 1, ["R1"], exp), ["robot:R1"], now=clock.now())
    later = iso(clock.now() + timedelta(minutes=9))
    assert repo.renew("task-000001", 1, later, now=clock.now())["expiry_ts"] == later
    assert repo.renew("task-000001", 1, exp, now=clock.now())["expiry_ts"] == later
    with pytest.raises(Conflict):
        repo.renew("task-000001", 2, later, now=clock.now())


def test_lease_lab_random_interleavings_never_double_hold():
    """Lease Lab (lite): random commit/renew/release/expire sequences must
    never leave one robot claimed by two tasks or let a version skip."""
    rng = random.Random(7)
    clock = FixedClock()
    repo = MemoryLeaseRepo()
    versions = {}
    for _ in range(3000):
        task = f"task-{rng.randint(1, 6):06d}"
        robot = f"R{rng.randint(1, 3)}"
        op = rng.choice(["new", "replace", "renew", "release", "tick"])
        exp = iso(clock.now() + timedelta(minutes=rng.randint(1, 10)))
        try:
            cur = repo.current(task)
            if op == "new" and cur is None and task not in versions:
                repo.commit(lease(task, 1, [robot], exp), [f"robot:{robot}"], now=clock.now())
                versions[task] = 1
            elif op == "replace" and cur:
                v = cur["version"] + 1
                repo.commit(lease(task, v, [robot], exp), [f"robot:{robot}"], now=clock.now())
                assert v == versions[task] + 1
                versions[task] = v
            elif op == "renew" and cur:
                repo.renew(task, cur["version"], exp, now=clock.now())
            elif op == "release":
                repo.release(task)
            elif op == "tick":
                clock.advance(rng.randint(1, 240))
        except Conflict:
            pass
        live = [(k, c["task_id"]) for k, c in repo.claims.items() if repo.holder(k, clock.now())]
        assert len({k for k, _ in live}) == len(live)  # one holder per key by construction
        for k, owner in live:  # the owner's current lease really includes that robot
            cur = repo.current(owner)
            assert cur and k == f"robot:{cur['assigned_robots'][0]}"


# -- scheduler ---------------------------------------------------------------------------------

def test_scheduler_orders_by_class_then_deadline_then_arrival():
    clock = FixedClock()
    s = Scheduler(pack.ranks)
    t = clock.now()
    s.push("r1", "routine", t + timedelta(minutes=5))
    s.push("u2", "urgent", t + timedelta(minutes=20))
    s.push("u1", "urgent", t + timedelta(minutes=10))
    s.push("r0", "routine", t + timedelta(minutes=5))
    assert [e.task_id for e in s.ordered()] == ["u1", "u2", "r1", "r0"]   # an urgent task never waits behind a routine one
    assert s.next_runnable(lambda e: e.priority == "routine").task_id == "r1"  # a blocked class doesn't hold up others
    s.remove("u1")
    assert "u1" not in s


# -- checker -------------------------------------------------------------------------------------

def view(clock=None, **kw):
    clock = clock or FixedClock()
    return FleetView(topo, FleetRegistry.load(clock, pack), MemoryLeaseRepo(), PolicyBook(pack), clock, pack, **kw)


SPEC = {"item": "wheelchair", "destination": "2A", "priority": "routine", "deadline_minutes": 60}


def test_checker_approves_a_sound_proposal_and_lists_checks():
    res = check(SPEC, Proposal("east-wing", ["R1"]), view())
    assert res.ok and {"cluster", "health", "robots", "capacity", "stock", "destination", "reachability"} <= set(res.checks)


@pytest.mark.parametrize("prop,rule", [
    (Proposal("mars-wing", ["R1"]), "cluster"),
    (Proposal("east-wing", ["R4"]), "robots"),          # a west robot
    (Proposal("west-wing", ["R4"]), "reachability"),    # 2A is in the east wing
])
def test_checker_vetoes(prop, rule):
    res = check(SPEC, prop, view())
    assert not res.ok and rule in [v["rule"] for v in res.violations]


def test_checker_rejects_stale_cluster_and_busy_robot():
    clock = FixedClock()
    v = view(clock)
    v.fleet.heartbeat("east-wing")
    clock.advance(60)
    assert "health" in [x["rule"] for x in check(SPEC, Proposal("east-wing", ["R1"]), v).violations]
    v2 = view()
    v2.repo.commit(lease("task-000009", 1, ["R1"], iso(v2.clock.now() + timedelta(minutes=5))), ["robot:R1"], now=v2.clock.now())
    assert "capacity" in [x["rule"] for x in check(SPEC, Proposal("east-wing", ["R1"]), v2).violations]


def test_checker_paediatric_pump_needs_confirmation():
    pump = {"item": "infusion_pump", "destination": "2B-12", "priority": "urgent", "deadline_minutes": 12}
    res = check(pump, Proposal("east-wing", ["R1"]), view())
    v = [x for x in res.violations if x["kind"] == "clarify"]
    assert v and v[0]["policy"] == "SOP-PAED-01" and "paediatric bed" in v[0]["question"]["question"]
    assert check({**pump, "variant_confirmed_by_requester": True}, Proposal("east-wing", ["R1"]), view()).ok
    assert check({**pump, "item": "infusion_pump_paediatric"}, Proposal("east-wing", ["R1"]), view()).ok


def test_isolation_holds_routine_but_urgent_may_still_enter():
    """PRD v5 SC-3: 'hold routine deliveries there, urgent ones can still go in'."""
    v = view()
    v.policies.declare("2A", "isolation", "infection-control")
    held = check(SPEC, Proposal("east-wing", ["R1"]), v)
    assert not held.ok and held.violations[0]["kind"] == "hold" and held.violations[0]["policy"] == "SOP-ISO-02"
    urgent = check({**SPEC, "priority": "urgent"}, Proposal("east-wing", ["R1"]), v)
    assert urgent.ok, urgent.violations


def test_a_blocked_event_zone_is_forbidden_for_everyone():
    v = view(extra_forbidden={"E1": "blocked: cart"})
    res = check({**SPEC, "priority": "urgent"}, Proposal("east-wing", ["R1"]), v)
    assert not res.ok and "reachability" in [x["rule"] for x in res.violations]


def test_a_named_source_needs_no_stock_and_is_routed_from():
    spec = {**SPEC, "item": "wheelchair", "source": "2A", "destination": "E-STORE"}
    v = view()
    for c in v.fleet.clusters.values():
        c["stock"]["wheelchair"] = 0           # nothing in stock, but the item is already on the ward
    res = check(spec, Proposal("east-wing", ["R1"]), v)
    assert res.ok and res.source_zone == "2A" and res.routes[0][0] == "2A"
