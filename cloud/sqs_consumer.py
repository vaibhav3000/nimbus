"""Consume edge messages from SQS (PRD Fig 4: IoT Core rule to SQS to the cloud; FR-8 "no duplicate actions").

The IoT rule drops each edge message (check-in, escalation) on the queue. This long-polls it and hands each message to
`handler(topic, data) -> (http_status, body)`, the same function the HTTP and MQTT paths use, so FR-8 de-duplication,
version fencing, validation and logging are identical whichever way a message arrived.

Delivery rules (at-least-once, so the handler must be idempotent, and it is: repeated message ids are ignored):
* a result below 500 (accepted, duplicate, refused as stale, or invalid) deletes the message: retrying cannot change it;
* a 5xx or an exception leaves it on the queue to reappear after the visibility timeout;
* after `max_receives` receives the message is deleted and counted as poison (a redrive policy to a dead-letter
  queue is the better home for it; this stops a bad message from looping forever if none is configured).
"""

from __future__ import annotations

import json
import logging
import threading
import time

log = logging.getLogger("nimbus.sqs")

EDGE_TYPES = {"CLARIFY", "UNEXPECTED_EVENT", "OUTPUT_READY"}


def classify(topic: str | None, body: dict) -> str | None:
    """"checkin", "escalation" or None, from the topic when there is one and from the message's shape otherwise."""
    if topic:
        last = topic.rsplit("/", 1)[-1]
        if last in ("checkin", "escalation"):
            return last
    if body.get("type") in EDGE_TYPES:
        return "escalation"
    if {"lease_id", "lease_version", "timestamp"} <= set(body):
        return "checkin"
    return None


class SQSConsumer:
    def __init__(self, queue_url: str, handler, *, client=None, region: str = "ap-south-1", wait_s: int = 10,
                 max_receives: int = 5, metrics=None):
        if client is None:
            import boto3
            client = boto3.client("sqs", region_name=region)
        self.url, self.handler, self.c = queue_url, handler, client
        self.wait_s, self.max_receives, self.metrics = wait_s, max_receives, metrics
        self.stats = {"received": 0, "handled": 0, "retried": 0, "poison": 0, "ignored": 0}
        self._stop = threading.Event()

    def poll_once(self) -> int:
        resp = self.c.receive_message(QueueUrl=self.url, MaxNumberOfMessages=10, WaitTimeSeconds=self.wait_s,
                                      AttributeNames=["ApproximateReceiveCount"], MessageAttributeNames=["All"])
        msgs = resp.get("Messages", [])
        for m in msgs:
            self.stats["received"] += 1
            self._one(m)
        return len(msgs)

    def _one(self, m: dict) -> None:
        receives = int(m.get("Attributes", {}).get("ApproximateReceiveCount", "1"))
        try:
            data = json.loads(m["Body"])
            topic = data.get("topic") if isinstance(data, dict) else None
            body = data["payload"] if isinstance(data, dict) and isinstance(data.get("payload"), dict) and topic else data
            attr = (m.get("MessageAttributes") or {}).get("topic", {}).get("StringValue")
            kind = classify(topic or attr, body) if isinstance(body, dict) else None
            if kind is None:
                self.stats["ignored"] += 1
                return self._delete(m)                       # not a message this service understands: do not loop on it
            if kind == "checkin" and "cluster_id" not in body and (topic or attr):
                body["cluster_id"] = (topic or attr).split("/")[1]
            status, _ = self.handler(topic or attr or f"nimbus/{body.get('cluster_id', '-')}/{kind}", body, kind)
            if status >= 500:
                raise RuntimeError(f"handler returned {status}")
            self.stats["handled"] += 1
            self._delete(m)
        except Exception as exc:
            if receives >= self.max_receives:
                self.stats["poison"] += 1
                log.warning("dropping a message that failed %d times: %s", receives, exc)
                self._delete(m)
            else:
                self.stats["retried"] += 1
                log.info("will retry (%s)", exc)

    def _delete(self, m: dict) -> None:
        self.c.delete_message(QueueUrl=self.url, ReceiptHandle=m["ReceiptHandle"])

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:                      # the queue being unreachable must not kill the thread
                log.warning("sqs poll failed: %s", exc)
                self._stop.wait(5)

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self.run, name="nimbus-sqs", daemon=True)
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()
