"""SQS consumer (edge messages by queue) and IoT publish robustness."""

import json

import pytest

from cloud.sqs_consumer import SQSConsumer, classify


class StubSQS:
    def __init__(self, msgs):
        self.msgs, self.deleted = msgs, []

    def receive_message(self, **kw):
        out, self.msgs = self.msgs, []
        return {"Messages": out}

    def delete_message(self, QueueUrl, ReceiptHandle):
        self.deleted.append(ReceiptHandle)


def msg(body, rh="r1", receives=1, attrs=None):
    m = {"Body": json.dumps(body), "ReceiptHandle": rh, "Attributes": {"ApproximateReceiveCount": str(receives)}}
    if attrs:
        m["MessageAttributes"] = attrs
    return m


def consumer(msgs, handler, **kw):
    return SQSConsumer("q", handler, client=StubSQS(msgs), wait_s=0, **kw)


def test_classification_by_topic_and_by_shape():
    assert classify("nimbus/east-wing/checkin", {}) == "checkin" and classify("nimbus/east-wing/escalation", {}) == "escalation"
    assert classify(None, {"type": "OUTPUT_READY"}) == "escalation"
    assert classify(None, {"lease_id": "l", "lease_version": 1, "timestamp": "t"}) == "checkin"
    assert classify(None, {"hello": 1}) is None


def test_accepted_duplicate_and_stale_messages_are_deleted_and_server_errors_are_retried():
    seen = []
    def handler(topic, body, kind):
        seen.append((topic, kind)); return {"a": (201, {}), "d": (200, {}), "s": (409, {}), "e": (500, {})}[body["k"]]
    c = consumer([msg({"k": k, "type": "OUTPUT_READY"}, rh=k) for k in "adse"], handler)
    c.poll_once()
    assert sorted(c.c.deleted) == ["a", "d", "s"] and c.stats["retried"] == 1 and c.stats["handled"] == 3


def test_a_message_that_keeps_failing_is_dropped_after_max_receives():
    c = consumer([msg({"type": "OUTPUT_READY"}, rh="bad", receives=5)], lambda t, b, k: (_ for _ in ()).throw(RuntimeError("boom")), max_receives=5)
    c.poll_once()
    assert c.c.deleted == ["bad"] and c.stats["poison"] == 1


def test_unrecognised_messages_are_deleted_not_looped_on():
    c = consumer([msg({"hello": "world"}, rh="x")], lambda t, b, k: (201, {}))
    c.poll_once()
    assert c.c.deleted == ["x"] and c.stats["ignored"] == 1


def test_iot_rule_envelope_and_topic_attribute_are_understood():
    got = []
    c = consumer([msg({"topic": "nimbus/west-wing/checkin", "payload": {"lease_id": "lease-t-v1", "lease_version": 1, "timestamp": "t", "message_id": "m"}}, rh="a"),
                  msg({"lease_id": "lease-t-v1", "lease_version": 1, "timestamp": "t", "message_id": "m2"}, rh="b",
                      attrs={"topic": {"StringValue": "nimbus/east-wing/checkin"}})],
                 lambda t, b, k: (got.append((t, b.get("cluster_id"), k)), (201, {}))[1])
    c.poll_once()
    assert got == [("nimbus/west-wing/checkin", "west-wing", "checkin"), ("nimbus/east-wing/checkin", "east-wing", "checkin")]


def test_a_message_from_the_queue_goes_through_the_same_pipeline_as_http(tmp_path):
    import server
    httpd = server.make_server("127.0.0.1", 0, tmp_path / "d")
    try:
        from conftest import http_json
        import threading
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        api = f"http://127.0.0.1:{httpd.server_address[1]}"
        _, b = http_json("POST", api + "/requests", {"text": "Deliver a wheelchair to 2B-3, routine.", "message_id": "sq1"})
        tid = b["task_id"]
        done = {"type": "OUTPUT_READY", "message_id": "sq-o", "lease_version": 1, "task_id": tid, "payload": {"result_ref": "s3://x/p.jpg"}}
        c = consumer([msg(done, rh="1"), msg(done, rh="2")], httpd.ingest)           # delivered twice (at-least-once)
        c.poll_once()
        assert c.c.deleted == ["1", "2"] and http_json("GET", api + f"/tasks/{tid}")[1]["status"] == "completed"
        timeline = http_json("GET", api + f"/tasks/{tid}/timeline")[1]["timeline"]
        assert sum(r["output"].get("event") == "OUTPUT_READY" for r in timeline) == 1     # FR-8: acted on once
    finally:
        httpd.shutdown()


def test_iot_publish_goes_out_at_qos1_and_a_failure_does_not_lose_the_message_or_the_request():
    from cloud.aws_store import DynamoStore

    class Iot:
        def __init__(self, boom=False): self.boom, self.sent = boom, []
        def publish(self, topic, qos, payload):
            if self.boom: raise RuntimeError("ThrottlingException")
            self.sent.append((topic, qos, json.loads(payload)))
    ok = DynamoStore(client=None, iot_client=Iot())
    ok.append_outbox({"topic": "nimbus/east-wing/lease", "payload": {"lease_id": "l"}})
    assert ok.iot.sent == [("nimbus/east-wing/lease", 1, {"lease_id": "l"})]
    bad = DynamoStore(client=None, iot_client=Iot(boom=True))
    bad.append_outbox({"topic": "nimbus/east-wing/lease", "payload": {"lease_id": "l"}})     # does not raise
    assert bad.outbox() == [{"topic": "nimbus/east-wing/lease", "payload": {"lease_id": "l"}}] and bad.iot_failures == 1


def test_consumer_against_an_emulated_queue(monkeypatch):
    moto = pytest.importorskip("moto")
    import boto3
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(k, "x")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-south-1")     # without it boto probes the metadata endpoint and waits
    with moto.mock_aws():
        sqs = boto3.client("sqs", region_name="ap-south-1")
        url = sqs.create_queue(QueueName="nimbus-escalations")["QueueUrl"]
        sqs.send_message(QueueUrl=url, MessageBody=json.dumps({"type": "OUTPUT_READY", "message_id": "m1"}))
        sqs.send_message(QueueUrl=url, MessageBody=json.dumps({"type": "OUTPUT_READY", "message_id": "m2"}))
        got = []
        c = SQSConsumer(url, lambda t, b, k: (got.append(b["message_id"]), (201, {}))[1], client=sqs, wait_s=0)
        assert c.poll_once() == 2 and sorted(got) == ["m1", "m2"]
        assert sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["ApproximateNumberOfMessages"])["Attributes"]["ApproximateNumberOfMessages"] == "0"
