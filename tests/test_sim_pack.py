"""The hospital_sim pack: the cloud planning on P1's Gazebo map.

The topology is generated from P1's files plus our overlay; these tests catch
drift when P1 updates their map, check the overlay still agrees with P1's own
wing and location data, and cover the generic code the pack needed (aliases, a
store shared by both clusters) and the cloud's tolerance of the edge's robot
digest (zones that are not map nodes, "busy" on every robot)."""

import importlib.util
import json
from pathlib import Path

import pytest

from cloud.pack import Pack
from cloud.topology import Topology
from cloud_helpers import make_orch, new_task
from test_packs import run_scenario

ROOT = Path(__file__).resolve().parents[1]
P1 = ROOT / "simulation" / "hospital_world" / "topology"
_spec = importlib.util.spec_from_file_location("build_sim_topology", ROOT / "scripts" / "build_sim_topology.py")
builder = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(builder)

SIM = Pack.load("hospital_sim")
TOPO = Topology(SIM)


# -- the generated topology ----------------------------------------------------------------------

def test_committed_topology_matches_a_rebuild_from_p1s_files():
    """Fails when P1 changes their map and nobody re-ran scripts/build_sim_topology.py."""
    assert builder.render(builder.build()) == (ROOT / "packs" / "hospital_sim" / "topology.json").read_text(encoding="utf-8")


def test_the_builder_keeps_p1s_speed_caps_and_edge_weights():
    p1 = json.loads((P1 / "hospital_topology.json").read_text())
    built = json.loads((ROOT / "packs" / "hospital_sim" / "topology.json").read_text())
    assert built["edges"] == p1["edges"]
    assert {n: a["speed_cap"] for n, a in built["nodes"].items()} == {n: a["speed_cap"] for n, a in p1["nodes"].items()}


def test_the_builder_refuses_an_overlay_node_that_is_not_in_p1s_map(tmp_path):
    overlay = json.loads((ROOT / "packs" / "hospital_sim" / "overlay.json").read_text())
    overlay["nodes"]["moon_base"] = {"kind": "store", "area": "shared"}
    path = tmp_path / "overlay.json"
    path.write_text(json.dumps(overlay))
    with pytest.raises(ValueError, match="moon_base"):
        builder.build(path)


def test_the_overlay_agrees_with_p1s_wings_and_ward_assignments():
    p1 = json.loads((P1 / "hospital_topology.json").read_text())
    locations = json.loads((P1 / "scenario_locations.json").read_text())["locations"]
    for node, attrs in p1["nodes"].items():
        assert TOPO.nodes[node]["area"] == attrs["wing"], f"{node}: overlay area differs from P1's wing"
    for stable_id, loc in locations.items():
        node = TOPO.nodes[loc["physical_node"]]
        if loc["logical_kind"] == "bed":     # a bed inherits its ward's flags and is resolved by the bed locator
            continue
        if loc["logical_kind"] in ("ward", "store"):
            assert node["kind"] == loc["logical_kind"], stable_id
        assert bool(node.get("paediatric")) == bool(loc.get("metadata", {}).get("paediatric")), stable_id
        for name in [stable_id, *loc["aliases"]]:
            assert TOPO.anchor_of(name) == loc["physical_node"], f"{name!r} does not resolve to {loc['physical_node']}"
    assert TOPO.anchor_of("2B-12") == "east_waiting_approach"   # P1's bed alias, through the bed locator


# -- aliases ---------------------------------------------------------------------------------------

def test_a_bed_and_a_ward_name_resolve_to_the_nodes_they_belong_to():
    assert TOPO.locate("2B-12") == {"zone": "east_waiting_approach", "anchor": "east_waiting_approach", "label": "bed"}
    assert TOPO.locate("Deliver a wheelchair to ward 3, routine")["zone"] == "south_room_approach"
    assert TOPO.locate("blankets to 2C")["zone"] == "west_waiting_approach"
    assert TOPO.anchor_of("2b") == "east_waiting_approach"          # case-insensitive
    assert TOPO.canonical_zone("2B") == "east_waiting_approach"
    assert TOPO.canonical_zone("E-NOWHERE") == "E-NOWHERE"           # unknown names are left alone


def test_unknown_places_and_out_of_range_beds_are_not_guessed():
    assert TOPO.locate("a pump to 2D") is None                       # no such ward
    assert TOPO.locate("a pump to 2B-25") is None                    # 2B has 20 beds
    assert TOPO.anchor_of("2D") is None


def test_the_prompt_context_names_each_node_with_its_label():
    ctx = make_orch(pack=SIM).build_context("a pump to 2B", "task-000001", []).to_prompt()
    assert "east_waiting_approach = ward 2B (area east, PAEDIATRIC, beds 1-20)" in ctx
    assert "south_room_approach = ward 3 (area shared, beds 1-30)" in ctx


# -- a store shared by both clusters -----------------------------------------------------------------

def test_both_clusters_start_from_the_one_shared_store():
    assert TOPO.home_of("east-wing") == TOPO.home_of("west-wing") == "south_equipment_approach"
    assert TOPO.source_of("east") == TOPO.source_of("west") == "south_equipment_approach"


def test_the_hospital_pack_still_uses_a_store_per_area():
    h = Topology(Pack.load("hospital"))
    assert (h.home_of("east-wing"), h.home_of("west-wing")) == ("E-STORE", "W-STORE")


def test_pack_validation_accepts_a_cluster_whose_store_is_outside_its_area():
    assert SIM.topology_doc["nodes"]["south_equipment_approach"]["area"] == "shared"   # loads without error


# -- speed ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("sid", ["T1", "SC-1", "SC-3-urgent", "SC-3-extract"])
def test_leases_never_exceed_the_slowest_zone_on_p1s_map(sid):
    sc = next(s for s in SIM.scenarios if s["id"] == sid)
    out = run_scenario("hospital_sim", sc)
    if out.kind != "leased":
        # A topology destination without a verified physical route must be
        # rejected rather than assigned a nominal speed limit.
        assert sid == "T1"
        return
    assert out.lease["speed_limit"] <= 1.0
    caps = [TOPO.speed_cap(z) for z in out.lease["allowed_zones"]]
    assert out.lease["speed_limit"] == min([SIM.doc["lease"]["speed_limit"], *[c for c in caps if c is not None]])


# -- the edge's robot digest -----------------------------------------------------------------------

def _digest(**robots):
    return {"robots": [{"id": rid, **vals} for rid, vals in robots.items()]}


def test_a_reported_zone_that_is_not_a_map_node_keeps_the_old_zone():
    orch = make_orch(pack=SIM)
    orch.fleet.heartbeat("east-wing", _digest(robot_1={"state": "idle", "zone": "hallway-central", "battery": 95.0}))
    r1 = orch.fleet.clusters["east-wing"]["robots"]["robot_1"]
    assert r1["zone"] == "south_equipment_approach" and r1["battery"] == 95.0   # battery is stored as sent
    orch.fleet.heartbeat("east-wing", _digest(robot_1={"state": "idle", "zone": "north_hall"}))
    assert orch.fleet.clusters["east-wing"]["robots"]["robot_1"]["zone"] == "north_hall"


def test_a_checkin_that_marks_free_robots_busy_does_not_stop_allocation():
    orch = make_orch(pack=SIM)
    orch.fleet.heartbeat("east-wing", _digest(robot_1={"state": "busy", "zone": "home"}))
    orch.fleet.heartbeat("west-wing", _digest(robot_2={"state": "busy", "zone": "transit"}))
    prompt = orch.build_context("a wheelchair to ward 2B", "task-000001", []).to_prompt()
    assert "robot_1=idle" in prompt and "robot_2=idle" in prompt        # busy only when a lease holds the robot
    assert orch.handle_request(new_task(), "Deliver a wheelchair to ward 2B, routine.").kind == "leased"


def test_the_extraction_task_still_leases_after_such_a_checkin():
    orch = make_orch(pack=SIM)
    orch.policies.declare("2C", "isolation")
    orch.fleet.heartbeat("west-wing", _digest(robot_2={"state": "busy", "zone": "hallway-central", "battery": 100.0}))
    out = orch.handle_request(new_task(), "Get the spare pumps out of 2C, urgent")
    assert out.kind == "leased" and out.lease["cluster_id"] == "west-wing"
    gotos = [s["params"]["to"] for s in out.lease["task_sequence"] if s["cmd"] == "go_to"]
    assert gotos == ["west_waiting_approach", "south_equipment_approach"]


def test_a_robot_that_holds_a_lease_is_shown_busy():
    orch = make_orch(pack=SIM)
    assert orch.handle_request(new_task(), "Deliver a wheelchair to ward 2B, routine.").kind == "leased"
    prompt = orch.build_context("another", "task-000002", []).to_prompt()
    assert "=busy" in prompt
