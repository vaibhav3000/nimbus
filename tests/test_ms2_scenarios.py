"""MS2 acceptance: SC-1, SC-2 and SC-3 end to end over HTTP with the fake edge, in C1, C3 and with the model down."""

import sys
import threading
from pathlib import Path

import pytest

import server
from conftest import http_json

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from edge_sim import EdgeSim  # noqa: E402

SC1 = "Need an infusion pump at 2B-12, urgent."
SC2 = "Move a monitor from 2A to 2C, routine"
SC3 = "Ward 2C is now isolation. Get the spare pumps out, hold routine deliveries there, urgent ones can still go in."


def _serve(request, tmp_path, monkeypatch, pack=None):
    if pack:
        monkeypatch.setenv("NIMBUS_PACK", pack)
    if request.param == "C3":
        monkeypatch.setenv("NIMBUS_MODE", "C3")
    elif request.param == "model-down":
        monkeypatch.setenv("NIMBUS_LLM", "bedrock")
        monkeypatch.setenv("NIMBUS_BEDROCK_MODEL", "no.such.model")      # every call fails: the rules must carry on
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
        monkeypatch.setenv("AWS_REGION", "ap-south-1")
        from cloud.llm import BedrockLLM, LLMError

        def down(self, **kw):
            raise LLMError("model unavailable")
        monkeypatch.setattr(BedrockLLM, "converse", down)
    httpd = server.make_server("127.0.0.1", 0, tmp_path / "data")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


@pytest.fixture(params=["C1", "C3", "model-down"])
def sys_api(request, tmp_path, monkeypatch):
    httpd = _serve(request, tmp_path, monkeypatch)
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture(params=["C1", "C3", "model-down"])
def sim_api(request, tmp_path, monkeypatch):
    """The same system planning on P1's Gazebo topology (pack hospital_sim)."""
    httpd = _serve(request, tmp_path, monkeypatch, pack="hospital_sim")
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def task(api, tid):
    return http_json("GET", api + f"/tasks/{tid}")[1]


def test_sc1_vague_urgent_request_is_clarified_then_leased_with_a_12_minute_deadline(sys_api):
    api = sys_api
    st, b = http_json("POST", api + "/requests", {"text": SC1, "message_id": "m-sc1"})
    assert st == 201 and b["status"] == "needs_clarification" and "paediatric" in b["question"]["question"].lower()
    st, b2 = http_json("POST", api + f"/requests/{b['task_id']}/answer", {"text": "Yes, a paediatric pump please.", "message_id": "a-sc1"})
    t = task(api, b["task_id"])
    assert t["status"] == "leased" and t["lease"]["cluster_id"] == "east-wing" and t["spec"]["item"] == "infusion_pump_paediatric"
    from cloud.clock import parse_iso
    minutes = (parse_iso(t["lease"]["deadline_ts"]) - parse_iso(t["spec"]["requested_at"])).total_seconds() / 60
    assert round(minutes) == 12


def test_sc2_corridor_blocked_then_a_west_gate_closes_and_the_last_leg_gets_a_new_lease_version(sim_api):
    """On P1's map: the east robot carries the item toward the hand-off; a corridor blocks (new hand-off, v2); leg 1 ends
    and the west robot takes over (v3); a gate into the west wing closes and leg 2 is planned another way (v4)."""
    api = sim_api
    text = "Move a monitor from 2B to 2C, routine"
    st, b = http_json("POST", api + "/requests", {"text": text, "message_id": "m-sc2"})
    tid = b["task_id"]
    t = task(api, tid)
    assert t["status"] == "leased" and len(t["legs"]) == 2 and t["lease"]["cluster_id"] == "east-wing"
    east = EdgeSim(api, "east-wing", block="east_mid_hall", robot_zone="east_waiting_approach")
    east.step()                                    # reads the lease, checks in, reports the corridor blocked, robot parked
    t = task(api, tid)
    assert t["lease"]["version"] == 2 and "east_mid_hall" in t["lease"]["forbidden_zones"]
    assert t["legs"][0]["destination"] == "north_hall"                    # hand-off moved to the other side of the wing
    done = {"type": "OUTPUT_READY", "message_id": "leg1-done", "lease_version": 2, "task_id": tid, "payload": {"result_ref": "s3://x/1"}}
    assert http_json("POST", api + "/escalations", done)[0] == 201
    t = task(api, tid)
    assert t["lease"]["version"] == 3 and t["lease"]["cluster_id"] == "west-wing" and t["leg_index"] == 1
    http_json("POST", api + "/fleet/heartbeat", {"cluster_id": "west-wing", "robots": [{"id": "robot_2", "state": "busy", "zone": "north_hall"}]})
    gate = {"type": "UNEXPECTED_EVENT", "message_id": "gate-1", "lease_version": 3, "task_id": tid,
            "payload": {"what_happened": "west_north_gate closed", "where": "west_north_gate", "robots_saw": ["door shut"],
                        "segments": [{"segment_id": "west_north_gate", "state": "closed"}]}}
    st, body = http_json("POST", api + "/escalations", gate)
    assert st == 201 and body["replanned"] is True and body["lease_version"] == 4
    t = task(api, tid)
    assert "west_north_gate" in t["lease"]["forbidden_zones"] and "west_mid_hall" in t["lease"]["allowed_zones"]
    _, tl = http_json("GET", api + f"/tasks/{tid}/timeline")
    assert {"react", "replan", "advance_leg", "plan_handover"} <= {r["output"].get("decision") for r in tl["timeline"]}
    notes = http_json("GET", api + "/notifications?audience=cluster")[1]["notifications"]
    assert any(n["type"] == "MAP" and "west_north_gate" in n["text"] for n in notes)
    assert http_json("POST", api + "/escalations", dict(gate, message_id="gate-stale", lease_version=1))[0] == 409


def test_sc3_isolation_by_sentence_holds_routine_admits_urgent_and_moves_the_pumps_out(sys_api):
    api = sys_api
    st, b = http_json("POST", api + "/requests", {"text": SC3, "message_id": "m-sc3"})
    parent = task(api, b["task_id"])
    assert st == 201 and parent["status"] == "completed" and len(parent["children"]) == 2
    west = EdgeSim(api, "west-wing", complete=True)
    west.step()                                    # the west edge runs both extraction leases to completion
    for cid in parent["children"]:
        assert task(api, cid)["status"] == "completed"
    _, fleet = http_json("GET", api + "/fleet")
    assert fleet["places"]["2C"] == {"infusion_pump": 0} or fleet["places"]["2C"] == {}
    routine = http_json("POST", api + "/requests", {"text": "Deliver a blanket to 2C, routine", "message_id": "m-r"})[1]
    urgent = http_json("POST", api + "/requests", {"text": "Deliver a blanket to 2C, urgent", "message_id": "m-u"})[1]
    assert task(api, routine["task_id"])["status"] == "queued" and task(api, urgent["task_id"])["status"] == "leased"
    _, tl = http_json("GET", api + f"/tasks/{b['task_id']}/timeline")
    assert any(r["output"].get("decision") == "declare_restriction" for r in tl["timeline"])
    ops = http_json("GET", api + "/notifications?audience=operator")[1]["notifications"]
    wards = http_json("GET", api + "/notifications?audience=place")[1]["notifications"]
    assert ops and wards


def test_sc3_on_the_gazebo_map_moves_the_pumps_out_with_the_west_robot(sim_api):
    api = sim_api
    st, b = http_json("POST", api + "/requests", {"text": SC3, "message_id": "m-sc3-sim"})
    parent = task(api, b["task_id"])
    assert parent["status"] == "completed" and len(parent["children"]) == 2
    states = sorted(task(api, c)["status"] for c in parent["children"])
    assert states == ["leased", "queued"]                       # one west robot on this map: the second pump waits its turn
    first = next(c for c in parent["children"] if task(api, c)["status"] == "leased")
    assert task(api, first)["lease"]["assigned_robots"] == ["robot_2"]
    EdgeSim(api, "west-wing", complete=True).step()             # the first finishes ...
    assert sorted(task(api, c)["status"] for c in parent["children"]) == ["completed", "leased"]   # ... and the second starts
    assert http_json("POST", api + "/requests", {"text": "Deliver a blanket to 2C, routine", "message_id": "m-r2"})[0] == 201
