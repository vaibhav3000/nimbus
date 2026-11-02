"""Tests for the switchable local MQTT transport and its canonical topics."""

import json
import os
import threading
import uuid
import pytest

import server
from cloud.clock import FixedClock
from mqtt_transport import (
    InMemoryBroker,
    MQTTTransport,
    StandinMQTTClient,
    canonical_topic,
    is_mqtt_enabled,
    topic_matches,
)
from tests.conftest import http_json


def test_canonical_topic_formatting():
    assert canonical_topic("east-wing", "lease") == "nimbus/east-wing/lease"
    assert canonical_topic("east-wing", "escalation") == "nimbus/east-wing/escalation"
    assert canonical_topic("east-wing", "checkin") == "nimbus/east-wing/checkin"
    assert canonical_topic("east-wing", "renewal") == "nimbus/east-wing/renewal"
    assert canonical_topic("east-wing", "probe") == "nimbus/east-wing/probe"
    with pytest.raises(ValueError):
        canonical_topic("east-wing", "invalid_type")


def test_topic_matching():
    assert topic_matches("nimbus/+/escalation", "nimbus/east-wing/escalation")
    assert topic_matches("nimbus/+/checkin", "nimbus/west-wing/checkin")
    assert not topic_matches("nimbus/+/checkin", "nimbus/west-wing/escalation")
    assert not topic_matches("nimbus/+/checkin", "nimbus/west-wing/checkin/extra")


def test_default_transport_leaves_mqtt_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv("NIMBUS_TRANSPORT", raising=False)
    assert not is_mqtt_enabled()

    httpd = server.make_server("127.0.0.1", 0, tmp_path / "data")
    assert httpd.transport is None

    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    status, body = http_json(
        "POST",
        f"{base}/requests",
        {"text": "deliver IV pole to room 2B-12", "message_id": f"msg-{uuid.uuid4().hex}"},
    )
    assert status == 201
    assert body["status"] in ("leased", "queued")

    outbox = (tmp_path / "data" / "outbox.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(outbox) >= 1
    row = json.loads(outbox[0])
    assert row["topic"].startswith("nimbus/")
    assert row["topic"].endswith("/lease")

    httpd.shutdown()
    httpd.server_close()


def test_mqtt_transport_lease_publishing(tmp_path, monkeypatch):
    broker = InMemoryBroker()
    received_messages = []

    def on_any_message(topic: str, payload_bytes: bytes):
        received_messages.append((topic, json.loads(payload_bytes.decode("utf-8"))))

    # Subscribe to canonical topics on the broker
    for t in ("lease", "renewal", "probe"):
        broker.subscribe(f"nimbus/+/{t}", on_any_message)

    monkeypatch.setenv("NIMBUS_TRANSPORT", "mqtt")
    monkeypatch.setenv("NIMBUS_MQTT_STANDIN", "1")
    assert is_mqtt_enabled()

    transport = MQTTTransport(force_standin=True, broker=broker)
    monkeypatch.setattr("server.make_mqtt_transport_from_env", lambda: transport)

    httpd = server.make_server("127.0.0.1", 0, tmp_path / "data")
    assert httpd.transport is not None

    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    # Submit task via HTTP request; cloud leases it and publishes to MQTT and outbox
    msg_id = f"msg-{uuid.uuid4().hex}"
    status, body = http_json(
        "POST",
        f"{base}/requests",
        {"text": "deliver IV pole to room 2B-12", "message_id": msg_id},
    )
    assert status == 201
    task_id = body["task_id"]

    # Verify message was published to MQTT on nimbus/{cluster_id}/lease
    lease_pubs = [m for m in received_messages if m[0].endswith("/lease")]
    assert len(lease_pubs) >= 1
    topic, payload = lease_pubs[0]
    assert topic == "nimbus/east-wing/lease"
    assert payload["lease_id"].startswith(f"lease-{task_id}-v")

    # Verify outbox.jsonl also contains the publication (mirrored)
    outbox = (tmp_path / "data" / "outbox.jsonl").read_text(encoding="utf-8").splitlines()
    assert any("nimbus/east-wing/lease" in line for line in outbox)

    httpd.shutdown()
    httpd.server_close()


def test_mqtt_transport_ingests_escalation_output_ready(tmp_path, monkeypatch):
    broker = InMemoryBroker()
    monkeypatch.setenv("NIMBUS_TRANSPORT", "mqtt")
    monkeypatch.setenv("NIMBUS_MQTT_STANDIN", "1")

    transport = MQTTTransport(force_standin=True, broker=broker)
    monkeypatch.setattr("server.make_mqtt_transport_from_env", lambda: transport)

    httpd = server.make_server("127.0.0.1", 0, tmp_path / "data")
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    # Create task
    msg_id = f"msg-{uuid.uuid4().hex}"
    status, body = http_json(
        "POST",
        f"{base}/requests",
        {"text": "deliver IV pole to room 2B-12", "message_id": msg_id},
    )
    assert status == 201
    task_id = body["task_id"]

    # Send OUTPUT_READY escalation via MQTT
    esc_msg_id = f"esc-{uuid.uuid4().hex}"
    esc_doc = {
        "type": "OUTPUT_READY",
        "task_id": task_id,
        "message_id": esc_msg_id,
        "lease_version": 1,
        "payload": {"result_ref": "s3://nimbus-test/photo.jpg"},
    }
    transport.publish("nimbus/east-wing/escalation", esc_doc)

    # Check task status over HTTP
    status, task_view = http_json("GET", f"{base}/tasks/{task_id}")
    assert status == 200
    assert task_view["status"] == "completed"
    assert task_view["result_ref"] == "s3://nimbus-test/photo.jpg"

    # Verify deduplication: publishing same esc_msg_id again does not add extra decision
    timeline_status, timeline = http_json("GET", f"{base}/tasks/{task_id}/timeline")
    assert timeline_status == 200
    row_count_before = len(timeline["timeline"])

    transport.publish("nimbus/east-wing/escalation", esc_doc)
    _, timeline_after = http_json("GET", f"{base}/tasks/{task_id}/timeline")
    assert len(timeline_after["timeline"]) == row_count_before

    httpd.shutdown()
    httpd.server_close()


def test_mqtt_transport_ingests_checkin_and_publishes_renewal(tmp_path, monkeypatch):
    broker = InMemoryBroker()
    received_renewals = []

    def on_renewal(topic: str, payload_bytes: bytes):
        received_renewals.append((topic, json.loads(payload_bytes.decode("utf-8"))))

    broker.subscribe("nimbus/+/renewal", on_renewal)

    monkeypatch.setenv("NIMBUS_TRANSPORT", "mqtt")
    monkeypatch.setenv("NIMBUS_MQTT_STANDIN", "1")

    transport = MQTTTransport(force_standin=True, broker=broker)
    monkeypatch.setattr("server.make_mqtt_transport_from_env", lambda: transport)

    httpd = server.make_server("127.0.0.1", 0, tmp_path / "data")
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    # Inject controllable clock so we can simulate time passing for lease renewal
    clock = FixedClock(httpd.orch.clock.now())
    httpd.orch.clock = clock

    # Create task
    msg_id = f"msg-{uuid.uuid4().hex}"
    status, body = http_json(
        "POST",
        f"{base}/requests",
        {"text": "deliver IV pole to room 2B-12", "message_id": msg_id},
    )
    assert status == 201
    task_id = body["task_id"]

    # Send initial check-in over MQTT
    checkin_msg_id_1 = f"chk-{uuid.uuid4().hex}"
    checkin_doc_1 = {
        "cluster_id": "east-wing",
        "lease_id": f"lease-{task_id}-v1",
        "lease_version": 1,
        "timestamp": "2026-10-25T12:00:00Z",
        "message_id": checkin_msg_id_1,
        "robots": [{"id": "R1", "state": "busy", "zone": "2B-12", "battery": 0.85}],
    }
    transport.publish("nimbus/east-wing/checkin", checkin_doc_1)

    # Verify decision log contains checkin row
    _, timeline = http_json("GET", f"{base}/tasks/{task_id}/timeline")
    checkin_rows = [r for r in timeline["timeline"] if r.get("output", {}).get("event") == "checkin"]
    assert len(checkin_rows) == 1

    # Advance the orchestrator clock by 61 minutes so the lease nears expiration
    clock.advance(61 * 60)

    # Send renewal check-in over MQTT
    checkin_msg_id_2 = f"chk-{uuid.uuid4().hex}"
    checkin_doc_2 = {
        "cluster_id": "east-wing",
        "lease_id": f"lease-{task_id}-v1",
        "lease_version": 1,
        "timestamp": "2026-10-25T13:01:00Z",
        "message_id": checkin_msg_id_2,
        "robots": [{"id": "R1", "state": "busy", "zone": "2B-12", "battery": 0.82}],
    }
    transport.publish("nimbus/east-wing/checkin", checkin_doc_2)

    # Verify renewal publication occurred on nimbus/east-wing/renewal
    assert len(received_renewals) >= 1
    renewal_topic, renewal_payload = received_renewals[-1]
    assert renewal_topic == "nimbus/east-wing/renewal"
    assert renewal_payload["lease_id"] == f"lease-{task_id}-v1"
    assert renewal_payload["version"] == 1

    # Check that outbox.jsonl also recorded the renewal publication
    outbox = (tmp_path / "data" / "outbox.jsonl").read_text(encoding="utf-8").splitlines()
    assert any("nimbus/east-wing/renewal" in line for line in outbox)

    httpd.shutdown()
    httpd.server_close()
