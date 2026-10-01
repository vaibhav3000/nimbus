"""API tests: SC-1 end to end, dedup (FR-8 shape), timeline replay (FR-20),
escalation ingress, schemas over HTTP, and cost counters — all through the
public HTTP surface only.
"""

import contracts
import standin
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


SC1_REQUEST = {
    "text": "Need an infusion pump at 2B-12, urgent.",
    "message_id": "msg-sc1-1",
}


def test_sc1_first_ask_needs_clarification(api):
    status, body = http_json("POST", api + "/requests", SC1_REQUEST)
    assert status == 201
    assert body["status"] == "needs_clarification"
    question = body["question"]
    assert question and question["question"] and question["possible_readings"]

    # The question the app shows is the CLARIFY payload shape (Table 2).
    clarify_escalation = {
        "type": "CLARIFY",
        "message_id": "msg-sc1-1-clarify",
        "lease_version": 1,
        "task_id": body["task_id"],
        "payload": question,
    }
    assert contracts.validate_document("escalation", clarify_escalation) == []

    # No lease may exist while the question is open.
    _, task = http_json("GET", api + f"/tasks/{body['task_id']}")
    assert task["lease"] is None


def test_sc1_answer_leases_and_timeline_records_it(api):
    _, body = http_json("POST", api + "/requests", SC1_REQUEST)
    task_id = body["task_id"]

    status, answered = http_json(
        "POST",
        api + f"/requests/{task_id}/answer",
        {"text": "Yes, a paediatric pump please.", "message_id": "msg-sc1-2"},
    )
    assert status == 201
    assert answered["status"] == "leased"
    assert answered["lease"]["version"] == 1
    assert contracts.validate_document("lease", answered["lease"]) == []
    assert answered["lease"]["priority"] == "urgent"
    assert answered["lease"]["deadline_ts"] <= answered["lease"]["expiry_ts"]

    _, tl = http_json("GET", api + f"/tasks/{task_id}/timeline")
    whos = [row["who"] for row in tl["timeline"]]
    assert whos[0] == "requester"
    assert "cloud" in whos and "rule" in whos
    answer_rows = [r for r in tl["timeline"] if r["inputs"].get("text") == "Yes, a paediatric pump please."]
    assert answer_rows and answer_rows[0]["who"] == "requester"


def leased_task(api) -> str:
    _, leased = http_json(
        "POST",
        api + "/requests",
        {"text": "Deliver a wheelchair to ward 3, routine.", "message_id": "msg-lease-1"},
    )
    return leased["task_id"]


def test_output_ready_completes_task_and_shows_result(api):
    task_id = leased_task(api)
    esc = {
        "type": "OUTPUT_READY",
        "message_id": "msg-out-1",
        "lease_version": 1,
        "task_id": task_id,
        "payload": {"result_ref": "s3://nimbus-outputs/task-000001/photo-1.jpg"},
    }
    status, body = http_json("POST", api + "/escalations", esc)
    assert status == 201
    assert body["accepted"] is True

    _, task = http_json("GET", api + f"/tasks/{task_id}")
    assert task["status"] == "completed"
    assert task["result_ref"] == esc["payload"]["result_ref"]

    _, tl = http_json("GET", api + f"/tasks/{task_id}/timeline")
    edge_rows = [r for r in tl["timeline"] if r["who"] == "edge"]
    assert edge_rows and edge_rows[-1]["output"]["event"] == "OUTPUT_READY"


def test_escalation_repeated_message_id_ignored(api):
    task_id = leased_task(api)
    esc = {
        "type": "OUTPUT_READY",
        "message_id": "msg-out-dup",
        "lease_version": 1,
        "task_id": task_id,
        "payload": {"result_ref": "s3://nimbus-outputs/x.jpg"},
    }
    status, first = http_json("POST", api + "/escalations", esc)
    status, second = http_json("POST", api + "/escalations", esc)
    assert status == 200 and second.get("duplicate") is True
    _, task = http_json("GET", api + f"/tasks/{task_id}")
    assert task["status"] == "completed"
    _, tl = http_json("GET", api + f"/tasks/{task_id}/timeline")
    assert len([r for r in tl["timeline"] if r["who"] == "edge"]) == 1


def test_escalation_for_unknown_task_404(api):
    esc = {
        "type": "UNEXPECTED_EVENT",
        "message_id": "msg-unknown-1",
        "lease_version": 1,
        "task_id": "task-999999",
        "payload": {"what_happened": "door closed", "where": "ward 3", "robots_saw": ["R1"]},
    }
    status, body = http_json("POST", api + "/escalations", esc)
    assert status == 404


def test_invalid_escalation_rejected_with_detail(api):
    task_id = leased_task(api)
    esc = {
        "type": "OUTPUT_READY",
        "message_id": "msg-bad-1",
        "lease_version": 1,
        "task_id": task_id,
        "payload": {"note": "no result_ref"},
    }
    status, body = http_json("POST", api + "/escalations", esc)
    assert status == 400
    assert any("result_ref" in e for e in body["detail"])


def test_schemas_served_over_http(api):
    for name in ("lease", "escalation", "robot_subtask", "decision_log_entry"):
        status, schema = http_json("GET", api + f"/schemas/{name}.json")
        assert status == 200
        assert schema["$id"].endswith(f"/{name}.schema.json")
    status, _ = http_json("GET", api + "/schemas/nope.json")
    assert status == 404


def test_failed_escalation_does_not_burn_message_id(api):
    """FR-8 dedup protects redeliveries of ACCEPTED messages; a message that
    failed validation never executed, so its honest retry must go through."""
    task_id = leased_task(api)
    bad = {
        "type": "OUTPUT_READY",
        "message_id": "msg-retry-1",
        "lease_version": 1,
        "task_id": task_id,
        "payload": {"note": "no result_ref"},
    }
    status, _ = http_json("POST", api + "/escalations", bad)
    assert status == 400
    good = dict(bad, payload={"result_ref": "s3://nimbus-outputs/x.jpg"})
    status, body = http_json("POST", api + "/escalations", good)
    assert status == 201 and body["accepted"] is True


def test_concurrent_requests_all_survive(api):
    """Task ids are allocated under the store lock: concurrent POSTs must all
    produce distinct, retrievable tasks (no silent overwrite)."""
    import concurrent.futures

    def send(i):
        return http_json(
            "POST",
            api + "/requests",
            {"text": f"Deliver blanket to ward 5, routine.", "message_id": f"msg-c-{i}"},
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(send, range(12)))
    task_ids = [body["task_id"] for _, body in results]
    assert len(set(task_ids)) == 12
    for task_id in task_ids:
        status, task = http_json("GET", api + f"/tasks/{task_id}")
        assert status == 200 and task["status"] == "leased"


def test_invalid_lease_is_single_500_and_id_not_burned(api, monkeypatch):
    """A stand-in fault must produce exactly one 500 response on the wire and
    must not burn the message_id: the same message can be retried afterwards."""
    import json
    import socket

    monkeypatch.setattr(
        standin, "build_lease",
        lambda task_id, text, version=1: {"lease_id": "broken"},
    )
    body = {"text": "Deliver a wheelchair to ward 3, routine.", "message_id": "msg-500-1"}

    # Raw-socket POST read to EOF, so a stray second response is visible.
    payload = json.dumps(body).encode()
    request = (
        "POST /requests HTTP/1.1\r\nHost: localhost\r\n"
        f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode() + payload
    port = int(api.rsplit(":", 1)[1])
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(request)
        wire = b""
        while True:
            part = sock.recv(4096)
            if not part:
                break
            wire += part

    status_lines = [line for line in wire.split(b"\r\n") if line.startswith(b"HTTP/")]
    assert len(status_lines) == 1, status_lines
    assert b" 500 " in status_lines[0]
    assert b"invalid lease" in wire

    monkeypatch.undo()
    status2, second = http_json("POST", api + "/requests", body)
    assert status2 == 201
    assert second.get("duplicate") is not True
    assert second["status"] == "leased"


def test_requester_app_served_at_root(api):
    import json as _json
    from urllib import request as urlreq

    with urlreq.urlopen(api + "/") as resp:
        html = resp.read().decode("utf-8")
        ctype = resp.headers["Content-Type"]
    assert resp.status == 200 and "text/html" in ctype
    assert "Requester App" in html  # the page renders tasks from GET /tasks

    with urlreq.urlopen(api + "/app.js") as resp:
        assert "application/javascript" in resp.headers["Content-Type"]
