"""HTTP-level tests for Person 3's behaviour: lease v2, stale fencing, health,
renewal, queueing, and the fake edge P4 asked for."""

import sys
from pathlib import Path

import contracts
from conftest import http_json

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from edge_sim import EdgeSim  # noqa: E402

SC1 = {"text": "Need an infusion pump at 2B-12, urgent.", "message_id": "msg-p3-sc1"}


def sc1_leased(api, suffix="a"):
    _, body = http_json("POST", api + "/requests", dict(SC1, message_id=f"msg-p3-{suffix}"))
    http_json("POST", api + f"/requests/{body['task_id']}/answer",
              {"text": "Yes, paediatric.", "message_id": f"msg-p3-ans-{suffix}"})
    return body["task_id"]


def esc(task_id, kind, version, payload, mid):
    return {"type": kind, "message_id": mid, "lease_version": version, "task_id": task_id, "payload": payload}


def test_sc1_task_view_shows_spec_and_priority(api):
    task_id = sc1_leased(api)
    _, task = http_json("GET", api + f"/tasks/{task_id}")
    assert task["status"] == "leased" and task["spec"]["item"] == "infusion_pump_paediatric"
    assert task["lease"]["cluster_id"] == "east-wing"


def test_replan_makes_v2_and_stale_v1_escalation_is_dropped(api):
    """The test P4 could not write: a real lease v2 path, then the stale-version guard."""
    task_id = sc1_leased(api)
    status, body = http_json("POST", api + f"/tasks/{task_id}/replan",
                             {"blocked_zones": ["E3"], "reason": "cart blocks E3", "message_id": "msg-rp-1"})
    assert status == 201 and body["lease"]["version"] == 2
    assert contracts.validate_document("lease", body["lease"]) == []

    stale = esc(task_id, "OUTPUT_READY", 1, {"result_ref": "s3://nimbus-outputs/old.jpg"}, "msg-stale-1")
    status, resp = http_json("POST", api + "/escalations", stale)
    assert status == 409 and resp["current"] == 2
    _, task = http_json("GET", api + f"/tasks/{task_id}")
    assert task["status"] == "leased" and task["result_ref"] is None  # v1 traffic had no effect

    fresh = esc(task_id, "OUTPUT_READY", 2, {"result_ref": "s3://nimbus-outputs/new.jpg"}, "msg-fresh-1")
    assert http_json("POST", api + "/escalations", fresh)[0] == 201

    _, out = http_json("GET", api + "/outbox")
    assert [r["payload"]["version"] for r in out["outbox"]] == [1, 2]
    _, tl = http_json("GET", api + f"/tasks/{task_id}/timeline")
    assert [r["output"].get("decision") for r in tl["timeline"] if r["who"] == "cloud"][-2:] == ["replan", "issue_lease"]


def test_replan_with_no_route_is_409_and_keeps_v1(api):
    task_id = sc1_leased(api)
    status, body = http_json("POST", api + f"/tasks/{task_id}/replan",
                             {"blocked_zones": ["E3", "E4"], "message_id": "msg-rp-2"})
    assert status == 409
    _, task = http_json("GET", api + f"/tasks/{task_id}")
    assert task["lease"]["version"] == 1


def test_unexpected_event_triggers_replan_without_operator(api):
    task_id = sc1_leased(api)
    payload = {"what_happened": "cart blocks corridor E3", "where": "corridor E3", "robots_saw": ["R1 saw cart"]}
    status, body = http_json("POST", api + "/escalations", esc(task_id, "UNEXPECTED_EVENT", 1, payload, "msg-ue-1"))
    assert status == 201 and body["replanned"] is True and body["lease_version"] == 2


def test_checkin_with_stale_version_is_told_the_current_one(api):
    task_id = sc1_leased(api)
    http_json("POST", api + f"/tasks/{task_id}/replan", {"blocked_zones": ["E3"], "message_id": "msg-rp-3"})
    status, body = http_json("POST", api + "/checkins", {
        "cluster_id": "east-wing", "lease_id": f"lease-{task_id}-v1", "lease_version": 1,
        "timestamp": "2026-10-18T09:00:00Z", "message_id": "msg-ci-stale"})
    assert status == 409 and body["current"] == 2


def test_fleet_health_follows_heartbeats(api):
    _, fleet = http_json("GET", api + "/fleet")
    assert {c["cluster_id"]: c["health"] for c in fleet["fleet"]} == {"east-wing": "unknown", "west-wing": "unknown"}
    status, hb = http_json("POST", api + "/fleet/heartbeat", {"cluster_id": "east-wing",
                           "robots": [{"id": "R1", "state": "failed"}]})
    assert status == 201 and hb["health"] == "healthy"
    assert http_json("POST", api + "/fleet/heartbeat", {"cluster_id": "mars"})[0] == 404
    _, fleet = http_json("GET", api + "/fleet")
    east = next(c for c in fleet["fleet"] if c["cluster_id"] == "east-wing")
    assert east["health"] == "healthy" and east["robots"][0]["state"] == "failed"
    _, body = http_json("POST", api + "/requests", {"text": "wheelchair to ward 3", "message_id": "msg-fl-1"})
    _, task = http_json("GET", api + f"/tasks/{body['task_id']}")
    assert task["lease"]["assigned_robots"] == ["R2"]  # R1 reported failed


def test_completion_frees_the_robot_and_the_next_queued_task_gets_it(api):
    ids = []
    for i in range(4):
        _, b = http_json("POST", api + "/requests", {"text": "blanket to ward 5, routine", "message_id": f"msg-q-{i}"})
        ids.append(b["task_id"])
    assert [http_json("GET", api + f"/tasks/{t}")[1]["status"] for t in ids] == ["leased"] * 3 + ["queued"]
    done = esc(ids[0], "OUTPUT_READY", 1, {"result_ref": "s3://nimbus-outputs/x.jpg"}, "msg-q-done")
    assert http_json("POST", api + "/escalations", done)[0] == 201
    _, fourth = http_json("GET", api + f"/tasks/{ids[3]}")
    assert fourth["status"] == "leased" and fourth["lease"]["version"] == 1


def test_cancel_frees_capacity_for_the_queue(api):
    ids = [http_json("POST", api + "/requests", {"text": "blanket to ward 5, routine", "message_id": f"msg-c-{i}"})[1]["task_id"] for i in range(4)]
    http_json("POST", api + f"/tasks/{ids[1]}/control", {"action": "cancel", "message_id": "msg-cancel-1"})
    assert http_json("GET", api + f"/tasks/{ids[3]}")[1]["status"] == "leased"


def test_edge_sim_gives_p4_their_timeline_rows(api):
    """P4's ask: read a lease from the outbox, send a check-in plus OUTPUT_READY."""
    task_id = sc1_leased(api)
    sim = EdgeSim(api, "east-wing", complete=True)
    assert sim.step() == 1 and sim.step() == 0   # answers each lease once
    _, task = http_json("GET", api + f"/tasks/{task_id}")
    assert task["status"] == "completed" and task["result_ref"].startswith("s3://")
    _, tl = http_json("GET", api + f"/tasks/{task_id}/timeline")
    events = [r["output"]["event"] for r in tl["timeline"] if r["who"] == "edge"]
    assert events == ["checkin", "OUTPUT_READY"]


def test_edge_sim_block_exercises_the_whole_replan_loop(api):
    task_id = sc1_leased(api)
    sim = EdgeSim(api, "east-wing", block="E3", complete=True)
    sim.step()           # sees v1, reports E3 blocked -> cloud issues v2
    sim.step()           # sees v2, checks in and completes
    _, task = http_json("GET", api + f"/tasks/{task_id}")
    assert task["status"] == "completed" and task["lease"]["version"] == 2


def test_bad_replan_requests_are_rejected(api):
    task_id = sc1_leased(api)
    assert http_json("POST", api + f"/tasks/{task_id}/replan", {"message_id": "m1"})[0] == 400
    assert http_json("POST", api + "/tasks/task-999999/replan", {"blocked_zones": ["E3"], "message_id": "m2"})[0] == 404
