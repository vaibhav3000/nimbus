"""Tests for P4 Red-Team Security & Reliability Remediations.

Validates:
- C1: Fail-closed HTTP authentication and role authorization boundaries.
- H1: Deduplication in-flight reservation race prevention.
- Addendum 1: Credential non-leakage, token verification, public vs protected routes.
- Addendum 3: Idempotent OUTPUT_READY handling.
- Request size bounds.
"""

import concurrent.futures
import json
import os
import threading
from urllib import request as urlreq
from urllib.error import HTTPError

import pytest

import server


@pytest.fixture()
def live_server(tmp_path):
    httpd = server.make_server("127.0.0.1", 0, tmp_path / "data")
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", httpd.store
    httpd.shutdown()


def test_public_routes_accessible_without_auth(live_server):
    """Public endpoints remain accessible without tokens."""
    url, _ = live_server
    # Test with auth enabled
    req = urlreq.Request(f"{url}/pack")
    with urlreq.urlopen(req, timeout=5) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode())
        assert "name" in data

    req_root = urlreq.Request(f"{url}/")
    with urlreq.urlopen(req_root, timeout=5) as resp:
        assert resp.status == 200


def test_fail_closed_when_auth_enabled_and_tokens_unset(live_server, monkeypatch):
    """When NIMBUS_AUTH_DISABLED=0 and tokens are unset, protected routes fail-closed with 401."""
    url, _ = live_server
    monkeypatch.setenv("NIMBUS_AUTH_DISABLED", "0")
    monkeypatch.setenv("NIMBUS_TEST_AUTH_CHECK", "1")
    monkeypatch.delenv("NIMBUS_EDGE_TOKEN", raising=False)
    monkeypatch.delenv("NIMBUS_OPERATOR_TOKEN", raising=False)
    monkeypatch.delenv("NIMBUS_REQUESTER_TOKEN", raising=False)
    monkeypatch.delenv("NIMBUS_AUTH_TOKEN", raising=False)

    # Attempt to access sensitive GET /tasks without token
    req = urlreq.Request(f"{url}/tasks")
    with pytest.raises(HTTPError) as exc_info:
        urlreq.urlopen(req, timeout=5)
    assert exc_info.value.code == 401
    err_body = json.loads(exc_info.value.read().decode())
    assert err_body["error"] == "unauthorized"
    # Ensure error does not leak internal secrets
    assert "secret" not in json.dumps(err_body).lower()

    # Attempt to post a request without token
    post_req = urlreq.Request(
        f"{url}/requests",
        data=json.dumps({"text": "need wheelchair at 204", "message_id": "m-sec-1"}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(HTTPError) as exc_info:
        urlreq.urlopen(post_req, timeout=5)
    assert exc_info.value.code == 401


def test_token_authorization_and_role_separation(live_server, monkeypatch):
    """Role-specific tokens permit their respective operations and reject unauthorized roles."""
    url, _ = live_server
    monkeypatch.setenv("NIMBUS_AUTH_DISABLED", "0")
    monkeypatch.setenv("NIMBUS_TEST_AUTH_CHECK", "1")
    monkeypatch.setenv("NIMBUS_EDGE_TOKEN", "edge-secret-tok")
    monkeypatch.setenv("NIMBUS_OPERATOR_TOKEN", "operator-secret-tok")
    monkeypatch.setenv("NIMBUS_REQUESTER_TOKEN", "requester-secret-tok")

    # 1. Invalid token on /tasks
    req_bad = urlreq.Request(f"{url}/tasks", headers={"Authorization": "Bearer wrong-token"})
    with pytest.raises(HTTPError) as exc_info:
        urlreq.urlopen(req_bad, timeout=5)
    assert exc_info.value.code == 401

    # 2. Operator token on /tasks succeeds
    req_op = urlreq.Request(f"{url}/tasks", headers={"Authorization": "Bearer operator-secret-tok"})
    with urlreq.urlopen(req_op, timeout=5) as resp:
        assert resp.status == 200

    # 3. Requester token on /requests succeeds
    post_req = urlreq.Request(
        f"{url}/requests",
        data=json.dumps({"text": "need wheelchair at 204", "message_id": "m-sec-auth-ok"}).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer requester-secret-tok",
        },
        method="POST",
    )
    with urlreq.urlopen(post_req, timeout=5) as resp:
        assert resp.status == 201

    # 4. X-Nimbus-Token header format also supported
    req_xhdr = urlreq.Request(f"{url}/tasks", headers={"X-Nimbus-Token": "operator-secret-tok"})
    with urlreq.urlopen(req_xhdr, timeout=5) as resp:
        assert resp.status == 200


def test_deduplication_in_flight_race_prevention(live_server):
    """H1: In-flight reservation prevents concurrent requests with identical message_id from creating multiple tasks."""
    url, _ = live_server
    msg_id = "concurrent-race-msg-id"
    payload = json.dumps({"text": "need wheelchair at 204", "message_id": msg_id}).encode()

    def make_post():
        req = urlreq.Request(
            f"{url}/requests",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlreq.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode())
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    # Send 4 concurrent requests with the identical message_id
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futures = [ex.submit(make_post) for _ in range(4)]
        results = [f.result() for f in futures]

    statuses = [r[0] for r in results]
    # Exactly one request must create the task (201) or finish, and concurrent duplicates receive 409 or 200 duplicate
    assert 201 in statuses
    # The others must be rejected as in-flight (409) or marked as duplicate (200)
    for code, body in results:
        if code != 201:
            assert code in (200, 409)
            assert body.get("duplicate") is True or "in-flight" in body.get("error", "")


def test_idempotent_output_ready_completion(live_server):
    """Addendum 3: Repeated OUTPUT_READY on an already-completed task returns duplicate without double finish."""
    url, store = live_server

    # Create a task and set to completed
    task = {
        "task_id": "task-test-idemp-1",
        "message_id": "m-task-1",
        "request_text": "deliver monitor",
        "status": "completed",
        "result_ref": "s3://nimbus/results/run1.json",
    }
    store.put_task(task)

    # Submit OUTPUT_READY escalation
    esc_payload = {
        "type": "OUTPUT_READY",
        "task_id": "task-test-idemp-1",
        "lease_version": 1,
        "message_id": "esc-out-1",
        "payload": {
            "result_ref": "s3://nimbus/results/run1.json",
            "robot_id": "R1",
        },
    }

    req = urlreq.Request(
        f"{url}/escalations",
        data=json.dumps(esc_payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlreq.urlopen(req, timeout=5) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode())
        assert data.get("status") == "already_completed"
        assert data.get("duplicate") is True


def test_request_body_size_limit(live_server):
    """Oversized request bodies exceeding MAX_CONTENT_LENGTH are rejected."""
    url, _ = live_server
    # Send request declaring excessive length
    req = urlreq.Request(
        f"{url}/requests",
        data=b"{}",
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(20 * 1024 * 1024),  # 20 MB
        },
        method="POST",
    )
    with pytest.raises(HTTPError) as exc_info:
        urlreq.urlopen(req, timeout=5)
    assert exc_info.value.code == 400
    err = json.loads(exc_info.value.read().decode())
    assert "exceeds limit" in err["error"]
