"""MS2 Task 5: a stop lease reaches the robots (FR-22) without changing the lease contract."""

import contracts
from cloud_helpers import make_orch, new_task
from conftest import http_json


def leased(orch, text="Deliver a wheelchair to 2B-3, routine."):
    t = new_task(1, text)
    out = orch.handle_request(t, text)
    t.update(spec=out.spec, lease=out.lease, reservation=out.reservation, status="leased")
    return t, out


def test_stand_down_validates_against_the_unchanged_lease_schema_and_is_a_higher_version():
    orch = make_orch()
    task, first = leased(orch)
    orch.fleet.heartbeat("east-wing", {"robots": [{"id": "R1", "state": "busy", "zone": "E2"}]})
    out = orch.stand_down(task, "operator cancel")
    assert out.kind == "stood_down" and out.lease["version"] == 2
    assert contracts.validate_document("lease", out.lease) == []
    assert out.lease["task_sequence"] == [{"cmd": "stop", "params": {}}] and out.lease["allowed_zones"] == ["E2"]
    assert out.publish[0][0] == "nimbus/east-wing/lease"
    assert set(out.lease) == set(first.lease)               # no field added or removed
    assert orch.repo.current(task["task_id"])["version"] == 2


def test_stale_version_is_refused_after_stand_down():
    import pytest
    from cloud.orchestrator import StaleVersion
    orch = make_orch()
    task, _ = leased(orch)
    orch.stand_down(task, "x")
    with pytest.raises(StaleVersion):
        orch.renew(task["task_id"], 1)


def test_stand_down_keeps_the_claim_until_the_caller_releases_it():
    orch = make_orch()
    task, _ = leased(orch)
    orch.stand_down(task, "pause")
    assert orch.repo.holder("robot:R1", orch.clock.now()) is not None
    orch.cancel(task)
    assert orch.repo.holder("robot:R1", orch.clock.now()) is None


def test_no_live_lease_means_nothing_to_stand_down():
    orch = make_orch()
    assert orch.stand_down(new_task(), "x").kind == "stand_down_failed"


def test_cancel_over_http_publishes_the_stop_lease(api):
    _, b = http_json("POST", api + "/requests", {"text": "Deliver a wheelchair to 2B-3, routine.", "message_id": "m-sd1"})
    tid = b["task_id"]
    assert http_json("POST", api + f"/tasks/{tid}/control", {"action": "cancel", "message_id": "c-sd1"})[0] == 201
    _, out = http_json("GET", api + "/outbox")
    leases = [i["payload"] for i in out["outbox"] if i["topic"].endswith("/lease") and i["payload"]["lease_id"].startswith(f"lease-{tid}-")]
    assert [l["version"] for l in leases] == [1, 2] and leases[1]["task_sequence"][0]["cmd"] == "stop"
    _, task = http_json("GET", api + f"/tasks/{tid}")
    assert task["status"] == "cancelled" and task["lease"]["version"] == 2


def test_pause_over_http_also_stops_the_robot(api):
    _, b = http_json("POST", api + "/requests", {"text": "Deliver a wheelchair to 2B-3, routine.", "message_id": "m-sd2"})
    tid = b["task_id"]
    assert http_json("POST", api + f"/tasks/{tid}/control", {"action": "pause", "message_id": "c-sd2"})[0] == 201
    _, task = http_json("GET", api + f"/tasks/{tid}")
    assert task["status"] == "paused" and task["lease"]["task_sequence"][0]["cmd"] == "stop"
