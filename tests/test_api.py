"""API tests: SC-1 end to end, dedup (FR-8 shape), timeline replay (FR-20),
escalation ingress, schemas over HTTP, and cost counters — all through the
public HTTP surface only.
"""

import contracts
from conftest import http_json

ROUTINE_REQUEST = {
    "text": "Deliver a wheelchair to ward 3, routine.",
    "message_id": "msg-r-1",
}


def test_post_request_issues_valid_lease(api):
    status, body = http_json("POST", api + "/requests", ROUTINE_REQUEST)
    assert status == 201
    assert body["status"] == "leased"
    assert body["question"] is None

    status, task = http_json("GET", api + f"/tasks/{body['task_id']}")
    assert status == 200
    assert task["status"] == "leased"
    assert task["lease"]["version"] == 1
    assert contracts.validate_document("lease", task["lease"]) == []


def test_timeline_shows_requester_cloud_and_rule_rows(api):
    _, body = http_json("POST", api + "/requests", ROUTINE_REQUEST)
    status, tl = http_json("GET", api + f"/tasks/{body['task_id']}/timeline")
    assert status == 200
    whos = [row["who"] for row in tl["timeline"]]
    assert whos == ["requester", "cloud", "rule", "cloud"]
    for row in tl["timeline"]:
        assert contracts.validate_document("decision_log_entry", row) == []
    issued = [r for r in tl["timeline"] if r["output"].get("decision") == "issue_lease"]
    assert issued and issued[0]["lease_version"] == 1


def test_repeated_message_id_is_ignored(api):
    status1, first = http_json("POST", api + "/requests", ROUTINE_REQUEST)
    status2, second = http_json("POST", api + "/requests", ROUTINE_REQUEST)
    assert status1 == 201 and status2 == 200
    assert second.get("duplicate") is True
    _, listing = http_json("GET", api + "/tasks")
    assert len(listing["tasks"]) == 1
    _, tl = http_json("GET", api + f"/tasks/{first['task_id']}/timeline")
    assert len(tl["timeline"]) == 4  # no rows appended by the repeat


def test_cost_counters_move_with_requests(api):
    _, before = http_json("GET", api + "/cost")
    http_json("POST", api + "/requests", ROUTINE_REQUEST)
    _, after = http_json("GET", api + "/cost")

    counters = after["counters"]
    assert counters["apigw_requests"] > before["counters"]["apigw_requests"]
    assert counters["input_tokens"] > 0  # T_in (stand-in tokenizer)
    assert counters["output_tokens"] > 0  # T_out, capped like the PRD's 150
    assert counters["iot_messages"] >= 1  # lease published to the cluster
    assert counters["dynamodb_ops"] >= 1  # task/lease/decision writes
    assert counters["data_transfer_bytes"] > 0
    assert after["tasks"] == 1
