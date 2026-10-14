"""DynamoDB-backed replacement for P4's file Store (same public surface).

tasks / decision_log / dedupe-ids / task-id counter live in DynamoDB; the
outbox publishes to AWS IoT Core when NIMBUS_IOT_ENDPOINT is set (QoS 1, the
PRD's at-least-once pipe) and is kept in memory for GET /outbox.

The message-id claim is a conditional put, so two identical deliveries racing
can no longer both pass (the gap P4's file store documents).
"""

from __future__ import annotations

import json
import os
import threading
import time

from .aws_dynamo import _n, _s, tables_from_env

MSG_TTL_S = 7 * 86400


class _SeenIds:
    def __init__(self, store: "DynamoStore"):
        self.store = store

    def __contains__(self, message_id: str) -> bool:
        item = self.store.c.get_item(TableName=self.store.t["state"], Key={"pk": _s(f"MSG#{message_id}")},
                                     ConsistentRead=True).get("Item")
        return item is not None


class DynamoStore:
    def __init__(self, client, names: dict | None = None, iot_client=None):
        self.c = client
        self.t = names or tables_from_env()
        self.iot = iot_client
        self.seen_message_ids = _SeenIds(self)
        self._outbox: list[dict] = []
        self._seq = 0
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls) -> "DynamoStore":
        import boto3
        region = os.environ.get("AWS_REGION", "ap-south-1")
        endpoint = os.environ.get("NIMBUS_IOT_ENDPOINT")
        iot = boto3.client("iot-data", region_name=region, endpoint_url=f"https://{endpoint}") if endpoint else None
        return cls(boto3.client("dynamodb", region_name=region), iot_client=iot)

    def next_task_id(self) -> str:
        r = self.c.update_item(TableName=self.t["state"], Key={"pk": _s("COUNTER#task")},
                               UpdateExpression="ADD #n :one", ExpressionAttributeNames={"#n": "n"},
                               ExpressionAttributeValues={":one": _n(1)},
                               ReturnValues="UPDATED_NEW")
        return f"task-{int(r['Attributes']['n']['N']):06d}"

    def get_task(self, task_id: str) -> dict | None:
        item = self.c.get_item(TableName=self.t["tasks"], Key={"task_id": _s(task_id)}, ConsistentRead=True).get("Item")
        return json.loads(item["doc"]["S"]) if item else None

    def all_tasks(self) -> list[dict]:
        out, kw = [], {}
        while True:
            page = self.c.scan(TableName=self.t["tasks"], ConsistentRead=True, **kw)
            out += [json.loads(i["doc"]["S"]) for i in page["Items"]]
            if "LastEvaluatedKey" not in page:
                return sorted(out, key=lambda t: t["task_id"])
            kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def put_task(self, task: dict) -> None:
        self.c.put_item(TableName=self.t["tasks"], Item={"task_id": _s(task["task_id"]), "doc": _s(json.dumps(task))})
        if task.get("message_id"):
            self.see_message_id(task["message_id"])

    def see_message_id(self, message_id: str) -> bool:
        """Atomic claim. Returns False if some delivery already claimed it."""
        try:
            self.c.put_item(TableName=self.t["state"],
                            Item={"pk": _s(f"MSG#{message_id}"), "ttl": _n(int(time.time()) + MSG_TTL_S)},
                            ConditionExpression="attribute_not_exists(pk)")
            return True
        except self.c.exceptions.ConditionalCheckFailedException:
            return False

    def append_decision(self, row: dict) -> None:
        with self._lock:
            self._seq += 1
            ts = f"{row['timestamp']}#{self._seq:08d}#{os.getpid()}"
        self.c.put_item(TableName=self.t["decision_log"],
                        Item={"task_id": _s(row["task_id"]), "ts": _s(ts), "row": _s(json.dumps(row))})

    def timeline(self, task_id: str) -> list[dict]:
        rows, kw = [], {}
        while True:
            page = self.c.query(TableName=self.t["decision_log"], ConsistentRead=True,
                                KeyConditionExpression="task_id = :t", ExpressionAttributeValues={":t": _s(task_id)}, **kw)
            rows += [json.loads(i["row"]["S"]) for i in page["Items"]]
            if "LastEvaluatedKey" not in page:
                return rows
            kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def append_outbox(self, entry: dict) -> None:
        with self._lock:
            self._outbox.append(entry)
        if self.iot:
            self.iot.publish(topic=entry["topic"], qos=1, payload=json.dumps(entry["payload"]).encode())

    def outbox(self) -> list[dict]:
        with self._lock:
            return list(self._outbox)
