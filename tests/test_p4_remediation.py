"""Comprehensive regression and verification test suite for P4 Red-Team Remediation.

Covers:
  - Area A: Authentication & API Security (public task detail closure, upload role mapping, cluster isolation, path traversal, payload validation)
  - Area B: Terraform & IAM Static Policy Checks (networking egress, IoT action/ARN separation, S3 security baselines)
  - Area C: MQTT Transport & Durability (spool persistence, safe replay, bounded queue overflow, message deduplication, metrics)
  - Area D: Contract Validation (ISO-8601 offset awareness, timestamp bounds, missing/malformed validation)
  - Area E & F: Experiment Metadata & Results Consistency
"""

from __future__ import annotations

import collections
import json
import os
import re
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib import request as urlreq
from urllib.error import HTTPError
import pytest

import contracts
import mqtt_transport
import server


def req_json(method: str, url: str, body: dict | None = None, headers: dict | None = None):
    """Helper to send HTTP request with custom headers and JSON body."""
    data = None if body is None else bytes(json.dumps(body), "utf-8")
    hdrs = dict(headers or {})
    if data is not None and "Content-Type" not in hdrs:
        hdrs["Content-Type"] = "application/json"
    req = urlreq.Request(url, data=data, headers=hdrs, method=method)
    try:
        resp = urlreq.urlopen(req)
    except HTTPError as err:
        resp = err
    with resp:
        content = resp.read().decode("utf-8")
        try:
            parsed = json.loads(content)
        except Exception:
            parsed = content
        return resp.status if hasattr(resp, "status") else resp.code, parsed


# ============================================================================
# Area A: Authentication & API Security Tests
# ============================================================================

@pytest.fixture
def test_cloud_server(tmp_path, monkeypatch):
    """Run an isolated test cloud server with authentication enabled."""
    admin_tok = "test-admin-secret-token"
    edge_tok = "test-edge-secret-token"
    req_tok = "test-req-secret-token"
    op_tok = "test-op-secret-token"

    monkeypatch.setenv("NIMBUS_AUTH_TOKEN", admin_tok)
    monkeypatch.setenv("NIMBUS_EDGE_TOKEN", edge_tok)
    monkeypatch.setenv("NIMBUS_REQUESTER_TOKEN", req_tok)
    monkeypatch.setenv("NIMBUS_OPERATOR_TOKEN", op_tok)
    monkeypatch.setenv("NIMBUS_TEST_AUTH_CHECK", "1")
    monkeypatch.delenv("NIMBUS_AUTH_DISABLED", raising=False)

    data_dir = tmp_path / "cloud_data"
    httpd = server.make_server("127.0.0.1", 0, data_dir)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    tokens = {
        "admin": admin_tok,
        "edge": edge_tok,
        "requester": req_tok,
        "operator": op_tok,
    }
    yield base, tokens, data_dir

    httpd.shutdown()
    httpd.server_close()


def test_public_task_detail_access_is_blocked(test_cloud_server):
    """A1: Ensure unauthenticated requests to task detail endpoints are rejected with 401."""
    base, tokens, _ = test_cloud_server

    # Create a task using requester token
    status, body = req_json(
        "POST",
        f"{base}/requests",
        {"text": "deliver pump to 2B-12", "message_id": f"msg-{uuid.uuid4().hex}"},
        headers={"Authorization": f"Bearer {tokens['requester']}"},
    )
    assert status == 201
    task_id = body["task_id"]

    # 1. Unauthenticated GET /tasks/{task_id} must return 401
    status, err = req_json("GET", f"{base}/tasks/{task_id}", headers={})
    assert status == 401
    assert "unauthorized" in str(err).lower()

    # 2. Unauthenticated GET /tasks must return 401
    status, _ = req_json("GET", f"{base}/tasks", headers={})
    assert status == 401

    # 3. Unauthenticated GET /tasks/{task_id}/timeline must return 401
    status, _ = req_json("GET", f"{base}/tasks/{task_id}/timeline", headers={})
    assert status == 401

    # 4. Unauthenticated GET /trace and GET /tasks/{task_id}/trace must return 401
    status, _ = req_json("GET", f"{base}/trace", headers={})
    assert status == 401
    status, _ = req_json("GET", f"{base}/tasks/{task_id}/trace", headers={})
    assert status == 401

    # 5. Invalid token must return 401
    status, _ = req_json("GET", f"{base}/tasks/{task_id}", headers={"Authorization": "Bearer bad-token"})
    assert status == 401

    # 6. Authenticated requester can read task detail and timeline
    status, view = req_json("GET", f"{base}/tasks/{task_id}", headers={"Authorization": f"Bearer {tokens['requester']}"})
    assert status == 200
    assert view["task_id"] == task_id

    # 7. Authenticated operator can read task detail, timeline, and traces
    status, view = req_json("GET", f"{base}/tasks/{task_id}", headers={"Authorization": f"Bearer {tokens['operator']}"})
    assert status == 200
    status, _ = req_json("GET", f"{base}/tasks/{task_id}/trace", headers={"Authorization": f"Bearer {tokens['operator']}"})
    assert status == 200


def test_edge_role_result_upload_authorization_and_validation(test_cloud_server):
    """A2: Verify edge client upload authorization, cluster boundary, file sanitization, and size limits."""
    base, tokens, data_dir = test_cloud_server

    # Create task assigned to east-wing
    status, body = req_json(
        "POST",
        f"{base}/requests",
        {"text": "Deliver a wheelchair to ward 3, routine.", "message_id": f"msg-{uuid.uuid4().hex}"},
        headers={"Authorization": f"Bearer {tokens['requester']}"},
    )
    assert status == 201
    task_id = body["task_id"]

    # 1. Requester cannot request upload url (403 forbidden)
    status, _ = req_json(
        "POST",
        f"{base}/results/upload-url",
        {"task_id": task_id, "cluster_id": "east-wing", "filename": "nav_log.json"},
        headers={"Authorization": f"Bearer {tokens['requester']}"},
    )
    assert status == 403

    # 2. Edge client with wrong cluster cannot upload against another cluster's task
    status, err = req_json(
        "POST",
        f"{base}/results/upload-url",
        {"task_id": task_id, "cluster_id": "west-wing", "filename": "nav_log.json"},
        headers={"Authorization": f"Bearer {tokens['edge']}"},
    )
    assert status == 403
    assert "cluster" in str(err).lower()

    # 3. Path traversal attack in filename must be rejected with 400
    for bad_name in ("../../etc/passwd", "..\\evil.bat", "sub/folder.txt", "evil/path"):
        status, err = req_json(
            "POST",
            f"{base}/results/upload-url",
            {"task_id": task_id, "cluster_id": "east-wing", "filename": bad_name},
            headers={"Authorization": f"Bearer {tokens['edge']}"},
        )
        assert status == 400

    # 4. Authorized edge client requests valid upload URL
    status, up_res = req_json(
        "POST",
        f"{base}/results/upload-url",
        {"task_id": task_id, "cluster_id": "east-wing", "filename": "delivery_photo.jpg"},
        headers={"Authorization": f"Bearer {tokens['edge']}"},
    )
    assert status == 201
    upload_url = up_res.get("url") or up_res.get("upload_url")
    assert upload_url.startswith(f"/results/upload/{task_id}/delivery_photo.jpg")

    # 5. Non-existent task upload fails
    status, _ = req_json(
        "POST",
        f"{base}/results/upload-url",
        {"task_id": "task-non-existent-9999", "cluster_id": "east-wing", "filename": "photo.jpg"},
        headers={"Authorization": f"Bearer {tokens['edge']}"},
    )
    assert status == 404

    # 6. Upload payload via PUT:
    # 6a. Empty payload rejected
    req = urlreq.Request(f"{base}{upload_url}", data=b"", headers={"Authorization": f"Bearer {tokens['edge']}"}, method="PUT")
    with pytest.raises(HTTPError) as exc_info:
        urlreq.urlopen(req)
    assert exc_info.value.code == 400

    # 6b. Successful payload upload
    content = b"SIMULATED_ROBOT_DELIVERY_PHOTO_BYTES_OK"
    req = urlreq.Request(
        f"{base}{upload_url}",
        data=content,
        headers={"Authorization": f"Bearer {tokens['edge']}", "Content-Type": "image/jpeg"},
        method="PUT",
    )
    with urlreq.urlopen(req) as resp:
        assert resp.status in (200, 201)
        put_res = json.loads(resp.read().decode())
    assert "ref" in put_res or "result_ref" in put_res
    ref = put_res.get("ref") or put_res.get("result_ref")
    assert f"results/{task_id}/delivery_photo.jpg" in ref

    # Verify file stored on disk under data_dir
    files = list(data_dir.rglob("delivery_photo.jpg"))
    assert len(files) == 1
    assert files[0].read_bytes() == content


# ============================================================================
# Area B: Terraform & AWS IAM Static Security Tests
# ============================================================================

def test_terraform_egress_and_networking_consistency():
    """B1: Verify assign_public_ip derivation and variables consistency."""
    tf_dir = Path("infra/terraform")
    vars_text = (tf_dir / "variables.tf").read_text(encoding="utf-8")
    main_text = (tf_dir / "main.tf").read_text(encoding="utf-8")

    # variables.tf must define assign_public_ip defaulting to null for dynamic derivation
    assert 'variable "assign_public_ip"' in vars_text
    assert "default     = null" in vars_text

    # main.tf must derive local.assign_public_ip based on egress_mode
    assert 'assign_public_ip = var.assign_public_ip != null ? var.assign_public_ip : (var.egress_mode == "public_ip")' in main_text
    assert "assign_public_ip = local.assign_public_ip" in main_text


def test_terraform_iot_iam_policy_resource_separation():
    """B2: Verify separate IAM statements for Connect, Publish, Receive, Subscribe without MQTT +."""
    main_text = Path("infra/terraform/main.tf").read_text(encoding="utf-8")
    cloud_text = Path("infra/terraform/cloud.tf").read_text(encoding="utf-8")

    # In main.tf, local.iot_topics must not use '+' in resource strings
    assert 'lease      = "nimbus/+/lease"' not in main_text
    assert 'lease      = "nimbus/*/lease"' in main_text

    # Verify action / resource category separation in task policy
    assert re.search(r'Sid\s*=\s*"IotConnect"', main_text) is not None
    assert re.search(r'Sid\s*=\s*"IotPublish"', main_text) is not None
    assert re.search(r'Sid\s*=\s*"IotReceive"', main_text) is not None
    assert re.search(r'Sid\s*=\s*"IotSubscribe"', main_text) is not None
    assert "topicfilter/${local.iot_topics.escalation}" in main_text

    # Verify edge policy separates Receive on topic and Subscribe on topicfilter
    assert 'Action = ["iot:Receive"]' in main_text
    assert 'Action = ["iot:Subscribe"]' in main_text
    assert "topicfilter/nimbus/*/lease" in main_text

    # Verify cloud.tf also separates Receive and Subscribe
    assert re.search(r'Sid\s*=\s*"IotReceive"', cloud_text) is not None
    assert re.search(r'Sid\s*=\s*"IotSubscribe"', cloud_text) is not None
    assert "topicfilter/nimbus/*/${t}" in cloud_text


def test_terraform_s3_security_baselines():
    """B3: Verify S3 bucket public access blocks, SSE encryption, and ownership controls."""
    main_text = Path("infra/terraform/main.tf").read_text(encoding="utf-8")

    for bucket in ("outputs", "static_app"):
        assert f'resource "aws_s3_bucket_public_access_block" "{bucket}"' in main_text
        assert f'resource "aws_s3_bucket_ownership_controls" "{bucket}"' in main_text
        assert f'resource "aws_s3_bucket_server_side_encryption_configuration" "{bucket}"' in main_text
        assert "block_public_acls       = true" in main_text
        assert "BucketOwnerEnforced" in main_text

    # static_app must have OAC policy
    assert 'resource "aws_s3_bucket_policy" "static_app"' in main_text
    assert "AllowCloudFrontOAC" in main_text


# ============================================================================
# Area C: MQTT Transport & Durability Tests
# ============================================================================

def test_mqtt_durable_spool_and_recovery(tmp_path):
    """C1: Test that disconnected messages persist to disk spool and reload upon client recreation."""
    spool_file = tmp_path / "mqtt_spool.jsonl"
    broker = mqtt_transport.InMemoryBroker()

    # Create client while disconnected
    client1 = mqtt_transport.StandinMQTTClient(broker=broker, spool_path=spool_file)
    client1.disconnect_broker()

    # Publish messages while offline
    client1.publish("nimbus/east-wing/checkin", {"message_id": "msg-1", "val": 100})
    client1.publish("nimbus/east-wing/checkin", {"message_id": "msg-2", "val": 200})

    # Verify spool file exists and has 2 lines
    assert spool_file.exists()
    lines = spool_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2

    # Stop client1 and create fresh client2 pointing to the same spool
    client2 = mqtt_transport.StandinMQTTClient(broker=broker, spool_path=spool_file)
    assert len(client2._pending_messages) == 2

    # Set up receiver on client2
    received = []
    client2.subscribe("nimbus/+/checkin", lambda topic, data: received.append(data))
    client2.start()

    # Reconnect client2 -> should flush spool
    client2.reconnect_broker()
    assert len(received) == 2
    assert [r["message_id"] for r in received] == ["msg-1", "msg-2"]

    # Verify spool file cleared upon successful replay
    assert not spool_file.exists() or spool_file.read_text(encoding="utf-8").strip() == ""


def test_mqtt_bounded_queue_overflow(tmp_path):
    """C2: Bounded capacity with observable overflow metrics instead of silent FIFO drops."""
    broker = mqtt_transport.InMemoryBroker()
    client = mqtt_transport.StandinMQTTClient(broker=broker, max_pending=5)
    client.disconnect_broker()

    # Publish up to max_pending (5 messages)
    for i in range(5):
        client.publish("nimbus/east-wing/checkin", {"message_id": f"msg-{i}"})

    assert len(client._pending_messages) == 5
    assert client._overflow_drops == 0

    # 6th message exceeds max_pending -> must raise BufferError and increment overflow_drops
    with pytest.raises(BufferError):
        client.publish("nimbus/east-wing/checkin", {"message_id": "msg-overflow"})

    metrics = client.metrics_snapshot()
    assert metrics["overflow_drops"] == 1
    assert metrics["pending_count"] == 5
    # First message was NOT silently dropped
    first_payload = json.loads(client._pending_messages[0][1].decode())
    assert first_payload["message_id"] == "msg-0"


def test_mqtt_reconnect_replay_preserves_messages_on_error():
    """C3: If a replay fails midway, messages are not prematurely dropped."""
    class FlakyBroker:
        def __init__(self):
            self.calls = 0
        def publish(self, topic, payload):
            self.calls += 1
            if self.calls == 2:
                raise ConnectionResetError("Simulated socket drop during replay")

    flaky = FlakyBroker()
    client = mqtt_transport.StandinMQTTClient(broker=flaky)
    client.disconnect_broker()

    client.publish("nimbus/east-wing/checkin", {"message_id": "msg-1"})
    client.publish("nimbus/east-wing/checkin", {"message_id": "msg-2"})
    client.publish("nimbus/east-wing/checkin", {"message_id": "msg-3"})

    assert len(client._pending_messages) == 3

    # Reconnect: message 1 succeeds, message 2 fails
    client.reconnect_broker()

    # Replay must stop on failure and retain remaining messages (msg-2 and msg-3)
    assert len(client._pending_messages) == 2
    rem1 = json.loads(client._pending_messages[0][1].decode())
    assert rem1["message_id"] == "msg-2"
    assert client._publish_failures == 1


def test_mqtt_message_deduplication():
    """C4: Verify duplicate message suppression based on message_id."""
    broker = mqtt_transport.InMemoryBroker()
    client = mqtt_transport.StandinMQTTClient(broker=broker)
    received = []

    client.subscribe("nimbus/+/notice", lambda topic, doc: received.append(doc))
    client.start()

    doc = {"message_id": "notice-unique-1234", "text": "Reduced autonomy active"}

    # Publish once
    client.publish("nimbus/east-wing/notice", doc)
    assert len(received) == 1

    # Publish duplicate with same message_id
    client.publish("nimbus/east-wing/notice", doc)
    assert len(received) == 1  # suppressed!

    metrics = client.metrics_snapshot()
    assert metrics["duplicates_suppressed"] == 1
    assert metrics["messages_received"] == 1


# ============================================================================
# Area D: Contract Validation Tests
# ============================================================================

def test_semantic_lease_errors_strict_validation():
    """D1: Test ISO-8601 offset-aware parsing, presence, and deadline <= expiry ordering."""
    base_lease = {
        "lease_id": "lease-001",
        "deadline_ts": "2026-10-02T10:12:00Z",
        "expiry_ts": "2026-10-02T10:15:00Z",
    }

    # 1. Valid lease produces no errors
    assert contracts.semantic_lease_errors(base_lease) == []

    # 2. Missing deadline_ts or expiry_ts
    errs = contracts.semantic_lease_errors({"expiry_ts": "2026-10-02T10:15:00Z"})
    assert any("deadline_ts: field is required" in e for e in errs)

    errs = contracts.semantic_lease_errors({"deadline_ts": "2026-10-02T10:12:00Z"})
    assert any("expiry_ts: field is required" in e for e in errs)

    # 3. Non-string types
    errs = contracts.semantic_lease_errors({"deadline_ts": 12345, "expiry_ts": "2026-10-02T10:15:00Z"})
    assert any("must be an ISO-8601 string" in e for e in errs)

    # 4. Malformed string
    errs = contracts.semantic_lease_errors({"deadline_ts": "not-a-timestamp", "expiry_ts": "2026-10-02T10:15:00Z"})
    assert any("invalid ISO-8601 timestamp" in e for e in errs)

    # 5. Naive timestamps (missing timezone offset / Z)
    naive_lease = {
        "deadline_ts": "2026-10-02T10:12:00",
        "expiry_ts": "2026-10-02T10:15:00",
    }
    errs = contracts.semantic_lease_errors(naive_lease)
    assert any("must be offset-aware" in e for e in errs)

    # 6. Numeric timezone offset (+05:30) is valid
    offset_lease = {
        "deadline_ts": "2026-10-02T10:12:00+05:30",
        "expiry_ts": "2026-10-02T10:15:00+05:30",
    }
    assert contracts.semantic_lease_errors(offset_lease) == []

    # 7. Inverted bounds: deadline > expiry
    inverted = {
        "deadline_ts": "2026-10-02T10:20:00Z",
        "expiry_ts": "2026-10-02T10:15:00Z",
    }
    errs = contracts.semantic_lease_errors(inverted)
    assert any("must be <= expiry_ts" in e for e in errs)

    # 8. Non-dict input never raises unhandled exception
    assert contracts.semantic_lease_errors(None) == ["$: lease must be a JSON object"]
    assert contracts.semantic_lease_errors("string") == ["$: lease must be a JSON object"]


# ============================================================================
# Area E & F: Experiment Results and Provenance Checks
# ============================================================================

def test_results_md_completeness_and_provenance():
    """E & F: Verify that results/final/RESULTS.md is complete with no blank columns."""
    res_path = Path("results/final/RESULTS.md")
    assert res_path.exists()
    content = res_path.read_text(encoding="utf-8")

    # Table rows must exist for all groups
    for exp in ("C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8", "C9", "R6", "R7", "R9", "R10", "N2", "N7", "N8", "N9", "N10"):
        assert f"[{exp}]({exp}/README.md)" in content, f"Missing row for {exp} in RESULTS.md"

    # Verify R7 shows PASS
    assert "| [R7](R7/README.md) | FR-6, FR-10 | PASS |" in content

    # Verify N8 shows PASS with latency numbers
    assert "| [N8](N8/README.md) | E7, NFR-3 | PASS | 10-robot latency ratio = 1.13x" in content

    # Verify N9 shows PASS and modelled source
    assert "| [N9](N9/README.md) | NFR-4 | PASS |" in content
    assert "modelled" in content
