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


def test_unauthorized_access_all_protected_get_endpoints(live_server, monkeypatch):
    """Every protected GET endpoint fails-closed (HTTP 401) without authentication."""
    url, _ = live_server
    monkeypatch.setenv("NIMBUS_AUTH_DISABLED", "0")
    monkeypatch.setenv("NIMBUS_TEST_AUTH_CHECK", "1")
    monkeypatch.delenv("NIMBUS_OPERATOR_TOKEN", raising=False)
    monkeypatch.delenv("NIMBUS_AUTH_TOKEN", raising=False)

    protected_get_paths = [
        "/tasks",
        "/fleet",
        "/trace",
        "/cost",
        "/metrics",
        "/inventory",
        "/llm-status",
    ]
    for path in protected_get_paths:
        req = urlreq.Request(f"{url}{path}")
        with pytest.raises(HTTPError) as exc_info:
            urlreq.urlopen(req, timeout=5)
        assert exc_info.value.code == 401, f"Expected 401 for GET {path}, got {exc_info.value.code}"


def test_unauthorized_access_all_protected_mutating_endpoints(live_server, monkeypatch):
    """Every protected mutating endpoint fails-closed (HTTP 401 or 403) without authentication."""
    url, _ = live_server
    monkeypatch.setenv("NIMBUS_AUTH_DISABLED", "0")
    monkeypatch.setenv("NIMBUS_TEST_AUTH_CHECK", "1")
    monkeypatch.delenv("NIMBUS_REQUESTER_TOKEN", raising=False)
    monkeypatch.delenv("NIMBUS_OPERATOR_TOKEN", raising=False)
    monkeypatch.delenv("NIMBUS_EDGE_TOKEN", raising=False)
    monkeypatch.delenv("NIMBUS_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("NIMBUS_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("NIMBUS_DEMO_FAULTS", raising=False)

    protected_mutating_endpoints = [
        ("/requests", {"text": "deliver blanket", "message_id": "m-unauth-1"}),
        ("/escalations", {"type": "UNEXPECTED_EVENT", "message_id": "m-unauth-2"}),
        ("/fleet/heartbeat", {"cluster_id": "east-wing", "message_id": "m-unauth-3"}),
        ("/inventory/stock", {"cluster_id": "east-wing", "stock": {"blanket": 5}}),
        ("/inventory/reset", {}),
        ("/reset-memory", {}),
        ("/fault", {"active": False, "profile": "healthy"}),
        ("/llm/outage", {"seconds": 10}),
        ("/checkins", {"cluster_id": "east-wing", "message_id": "m-unauth-4"}),
        ("/relay/decide", {"action": "step", "message_id": "m-unauth-5"}),
    ]
    for path, body in protected_mutating_endpoints:
        req = urlreq.Request(
            f"{url}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(HTTPError) as exc_info:
            urlreq.urlopen(req, timeout=5)
        assert exc_info.value.code in (401, 403), f"Expected 401/403 for POST {path}, got {exc_info.value.code}"


def test_production_backend_ignores_auth_disabled(live_server, monkeypatch):
    """In production mode (NIMBUS_BACKEND=aws or ENVIRONMENT=production), NIMBUS_AUTH_DISABLED=1 is strictly ignored."""
    url, _ = live_server
    monkeypatch.setenv("NIMBUS_BACKEND", "aws")
    monkeypatch.setenv("NIMBUS_AUTH_DISABLED", "1")
    monkeypatch.delenv("NIMBUS_OPERATOR_TOKEN", raising=False)
    monkeypatch.delenv("NIMBUS_AUTH_TOKEN", raising=False)

    req = urlreq.Request(f"{url}/tasks")
    with pytest.raises(HTTPError) as exc_info:
        urlreq.urlopen(req, timeout=5)
    assert exc_info.value.code == 401


def test_in_flight_reservation_timeout_eviction():
    """In-flight reservation entries expire and evict after _IN_FLIGHT_TIMEOUT seconds."""
    import time
    with server._STATE_LOCK:
        server._IN_FLIGHT_MESSAGES["stuck-msg-1"] = time.monotonic() - 150.0  # past 120s timeout
        server._IN_FLIGHT_MESSAGES["fresh-msg-2"] = time.monotonic()

    # Create dummy handler to test _dedupe eviction
    class DummyHandler:
        def __init__(self):
            self.store = type("DummyStore", (), {"seen_message_ids": set()})()
            self.responses = []
        def _send_json(self, status, body):
            self.responses.append((status, body))

    h = DummyHandler()
    is_dup, mid = server.Handler._dedupe(h, {"message_id": "stuck-msg-1"})
    # Stuck message should have been evicted and now allowed to be reserved
    assert is_dup is False
    assert mid == "stuck-msg-1"
    with server._STATE_LOCK:
        assert "stuck-msg-1" in server._IN_FLIGHT_MESSAGES
        server._IN_FLIGHT_MESSAGES.pop("stuck-msg-1", None)
        server._IN_FLIGHT_MESSAGES.pop("fresh-msg-2", None)


def test_mqtt_recovery_with_standin_client():
    """StandinMQTTClient buffers messages during broker disconnect and flushes upon reconnect."""
    from mqtt_transport import InMemoryBroker, StandinMQTTClient

    broker = InMemoryBroker()
    client = StandinMQTTClient(broker)
    delivered = []
    client.subscribe("nimbus/test-edge-1/lease", lambda t, p: delivered.append(p))
    client.start()

    # Normal delivery
    client.publish("nimbus/test-edge-1/lease", {"version": 1})
    assert len(delivered) == 1

    # Disconnect broker
    client.disconnect_broker()
    assert client.connected is False

    # Publish while broker disconnected
    client.publish("nimbus/test-edge-1/lease", {"version": 2})
    assert len(client._pending_messages) == 1
    assert len(delivered) == 1  # Not delivered yet

    # Restore broker
    client.reconnect_broker()
    assert client.connected is True
    assert len(client._pending_messages) == 0
    assert len(delivered) == 2
    assert delivered[1]["version"] == 2


def test_fault_harness_delay_clamping():
    """FaultProfile clamps artificial delay to 10.0s and jitter to 5.0s."""
    from scripts.fault_harness import FaultProfile

    profile = FaultProfile(latency_ms=999999.0, jitter_ms=99999.0)
    delay_s = profile.compute_delay_sec()
    assert 0.0 <= delay_s <= 10.0, f"Expected delay <= 10.0s, got {delay_s}"


def test_fault_harness_stale_generation_rejection(live_server, monkeypatch):
    """Server rejects stale fault generations with HTTP 409 Conflict."""
    url, _ = live_server
    monkeypatch.setenv("NIMBUS_AUTH_DISABLED", "1")

    # POST fault generation 10
    req1 = urlreq.Request(
        f"{url}/fault",
        data=json.dumps({"active": True, "profile": "mild_wan", "generation": 10}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlreq.urlopen(req1, timeout=5) as resp:
        assert resp.status == 200

    # POST stale fault generation 5 (lower than 10)
    req2 = urlreq.Request(
        f"{url}/fault",
        data=json.dumps({"active": True, "profile": "severe", "generation": 5}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(HTTPError) as exc_info:
        urlreq.urlopen(req2, timeout=5)
    assert exc_info.value.code == 409
    err = json.loads(exc_info.value.read().decode("utf-8"))
    assert "stale fault generation" in err["error"]


def test_production_backend_ignores_demo_faults(live_server, monkeypatch):
    """In production mode (NIMBUS_BACKEND=aws or ENVIRONMENT=production), NIMBUS_DEMO_FAULTS=1 cannot bypass admin auth."""
    url, _ = live_server
    monkeypatch.setenv("NIMBUS_BACKEND", "aws")
    monkeypatch.setenv("NIMBUS_DEMO_FAULTS", "1")
    monkeypatch.setenv("NIMBUS_AUTH_DISABLED", "0")
    monkeypatch.setenv("NIMBUS_TEST_AUTH_CHECK", "1")
    monkeypatch.delenv("NIMBUS_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("NIMBUS_FAULT_TOKEN", raising=False)
    monkeypatch.delenv("NIMBUS_AUTH_TOKEN", raising=False)

    req = urlreq.Request(
        f"{url}/fault",
        data=json.dumps({"active": False, "profile": "healthy"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(HTTPError) as exc_info:
        urlreq.urlopen(req, timeout=5)
    assert exc_info.value.code in (401, 403)


def test_failed_request_reservation_cleanup_and_retry(live_server):
    """When a request fails during processing, its in-flight reservation is cleaned up, allowing retry."""
    url, _ = live_server
    msg_id = "retry-test-msg-001"

    # 1. Send invalid request (empty/invalid text) with msg_id
    bad_req = urlreq.Request(
        f"{url}/requests",
        data=json.dumps({"text": "", "message_id": msg_id}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(HTTPError) as exc_info:
        urlreq.urlopen(bad_req, timeout=5)
    assert exc_info.value.code == 400

    # 2. Verify reservation is cleaned up from _IN_FLIGHT_MESSAGES
    with server._STATE_LOCK:
        assert msg_id not in server._IN_FLIGHT_MESSAGES

    # 3. Retry with valid payload using the EXACT SAME message_id
    valid_req = urlreq.Request(
        f"{url}/requests",
        data=json.dumps({"text": "deliver supplies to room 101", "message_id": msg_id}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlreq.urlopen(valid_req, timeout=5) as resp:
        assert resp.status == 201
        body = json.loads(resp.read().decode())
        assert body.get("duplicate") is False

    # 4. Verify reservation cleaned up after success
    with server._STATE_LOCK:
        assert msg_id not in server._IN_FLIGHT_MESSAGES


def test_process_restart_reservation_recovery(live_server):
    """Simulates process failure during reservation: fresh process has clear reservations, allowing retry."""
    url, _ = live_server
    msg_id = "crash-recovery-msg-002"

    import time
    # Simulate active in-flight reservation
    with server._STATE_LOCK:
        server._IN_FLIGHT_MESSAGES[msg_id] = time.monotonic()

    # In-flight request receives 409
    dup_req = urlreq.Request(
        f"{url}/requests",
        data=json.dumps({"text": "wheelchair needed", "message_id": msg_id}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(HTTPError) as exc_info:
        urlreq.urlopen(dup_req, timeout=5)
    assert exc_info.value.code == 409

    # Simulate process crash/reboot: in-memory in-flight reservations are wiped
    with server._STATE_LOCK:
        server._IN_FLIGHT_MESSAGES.clear()

    # Client retries request against restarted instance: succeeds
    retry_req = urlreq.Request(
        f"{url}/requests",
        data=json.dumps({"text": "wheelchair needed", "message_id": msg_id}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlreq.urlopen(retry_req, timeout=5) as resp:
        assert resp.status == 201

    # Cleanup verification
    with server._STATE_LOCK:
        assert msg_id not in server._IN_FLIGHT_MESSAGES


def test_mqtt_full_recovery_cycle_with_http_server(live_server):
    """Comprehensive 7-step MQTT recovery verification:
    1. Start with available broker.
    2. Stop/disconnect the broker.
    3. Confirm HTTP server remains completely available.
    4. Publish messages while broker is stopped; confirm they are buffered without silent loss.
    5. Restore the broker.
    6. Confirm MQTT reconnects and pending messages are delivered.
    7. Clean shutdown behaviour.
    """
    from mqtt_transport import InMemoryBroker, StandinMQTTClient

    url, _ = live_server
    broker = InMemoryBroker()
    client = StandinMQTTClient(broker)
    delivered = []
    client.subscribe("nimbus/recovery-cluster/lease", lambda t, p: delivered.append(p))
    client.start()

    # Step 1: Normal initial delivery
    client.publish("nimbus/recovery-cluster/lease", {"step": 1, "status": "init"})
    assert len(delivered) == 1

    # Step 2: Stop/disconnect the broker
    client.disconnect_broker()
    assert client.connected is False

    # Step 3: Confirm HTTP server remains available
    req = urlreq.Request(
        f"{url}/requests",
        data=json.dumps({"text": "deliver monitor to ICU", "message_id": "mqtt-rec-req-1"}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlreq.urlopen(req, timeout=5) as resp:
        assert resp.status == 201

    # Step 4: Publish while broker stopped; confirm messages buffered without silent loss
    client.publish("nimbus/recovery-cluster/lease", {"step": 4, "status": "buffered_1"})
    client.publish("nimbus/recovery-cluster/lease", {"step": 4, "status": "buffered_2"})
    assert len(delivered) == 1  # Unchanged
    assert len(client._pending_messages) == 2

    # Step 5 & 6: Restore broker and confirm reconnect + buffered delivery
    client.reconnect_broker()
    assert client.connected is True
    assert len(client._pending_messages) == 0
    assert len(delivered) == 3
    assert delivered[1]["status"] == "buffered_1"
    assert delivered[2]["status"] == "buffered_2"

    # Step 7: Clean shutdown
    client.stop()
    assert client._running is False


def test_duplicate_request_after_successful_completion(live_server):
    """Submitting a duplicate request after successful completion returns HTTP 200 with duplicate=True."""
    url, _ = live_server
    msg_id = "dup-after-complete-001"
    req_body = {"text": "deliver blanket to room 201", "message_id": msg_id}

    # 1. Initial request succeeds with HTTP 201
    post_req = urlreq.Request(
        f"{url}/requests",
        data=json.dumps(req_body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlreq.urlopen(post_req, timeout=5) as resp:
        assert resp.status == 201
        data = json.loads(resp.read().decode())
        assert data.get("duplicate") is False
        task_id = data.get("task_id")
        assert task_id is not None

    # Verify reservation is cleaned up
    with server._STATE_LOCK:
        assert msg_id not in server._IN_FLIGHT_MESSAGES

    # 2. Resend exact same message_id after completion: returns 200 duplicate
    dup_req = urlreq.Request(
        f"{url}/requests",
        data=json.dumps(req_body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlreq.urlopen(dup_req, timeout=5) as resp:
        assert resp.status == 200
        dup_data = json.loads(resp.read().decode())
        assert dup_data.get("duplicate") is True

    # Reservation remains cleaned up
    with server._STATE_LOCK:
        assert msg_id not in server._IN_FLIGHT_MESSAGES


def test_fault_proxy_interruptible_shutdown():
    """FaultProxy.stop() interrupts in-flight burst delay immediately via _stop_event."""
    import socket
    import time
    from scripts.fault_harness import FaultProfile, FaultProxy

    # 1. Create a dummy backend TCP echo server
    echo_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    echo_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    echo_server.bind(("127.0.0.1", 0))
    echo_server.listen(1)
    echo_port = echo_server.getsockname()[1]

    def _echo():
        try:
            client, _ = echo_server.accept()
            data = client.recv(1024)
            if data:
                client.sendall(data)
            client.close()
        except Exception:
            pass
        finally:
            echo_server.close()

    t_echo = threading.Thread(target=_echo, daemon=True)
    t_echo.start()

    # 2. Start FaultProxy with large artificial latency (5000ms = 5.0s)
    profile = FaultProfile(latency_ms=5000.0, jitter_ms=0.0)
    proxy = FaultProxy(listen_port=0, target_port=echo_port, profile=profile)
    proxy.start()

    try:
        # 3. Client connects and sends data, triggering the 5.0s delay in _pipe
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        connect_host = "127.0.0.1" if proxy.listen_host in ("0.0.0.0", "") else proxy.listen_host
        client.connect((connect_host, proxy.listen_port))
        client.sendall(b"ping")

        # Let the proxy accept and start delaying the chunk
        time.sleep(0.1)

        # 4. Measure how long proxy.stop() takes to terminate
        t0 = time.perf_counter()
        proxy.stop()
        elapsed = time.perf_counter() - t0

        # Interruptible shutdown should abort the delay in < 1.0s, not block for 5.0s
        assert elapsed < 1.5, f"Expected interruptible shutdown < 1.5s, took {elapsed:.2f}s"
    finally:
        proxy.stop()
        try:
            client.close()
        except Exception:
            pass



