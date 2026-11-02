"""Small durable key-value store for the cloud's own state that is not a lease: closures, restrictions, notices,
robot status and memory per place (PRD FR-17, FR-19; DynamoDB "robot status, map state").

Three implementations behind one interface. `MemoryDurable` (tests, no persistence across processes), `FileDurable`
(local runs: one JSON file in the data folder) and `DynamoDurable` (AWS: rows in the state table, robot status in the
robot_status table). A restart reloads what was saved, so a restriction declared before it is still in force after.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path


class MemoryDurable:
    def __init__(self):
        self._lock = threading.Lock()
        self._d: dict[str, dict[str, dict]] = {}

    def put(self, kind: str, key: str, value: dict, ttl_s: int | None = None) -> None:
        with self._lock:
            self._d.setdefault(kind, {})[key] = value

    def delete(self, kind: str, key: str) -> None:
        with self._lock:
            self._d.get(kind, {}).pop(key, None)

    def load(self, kind: str) -> dict[str, dict]:
        with self._lock:
            return {k: dict(v) for k, v in self._d.get(kind, {}).items()}

    def put_robot(self, robot_id: str, rec: dict) -> None:
        self.put("robot", robot_id, rec)


class FileDurable(MemoryDurable):
    def __init__(self, path: Path):
        super().__init__()
        self.path = Path(path)
        if self.path.exists():
            try:
                self._d = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._d = {}          # a damaged file must not stop the service: start empty, rewrite on next change

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._d), encoding="utf-8")
        os.replace(tmp, self.path)

    def put(self, kind, key, value, ttl_s=None):
        super().put(kind, key, value, ttl_s)
        with self._lock:
            self._save()

    def delete(self, kind, key):
        super().delete(kind, key)
        with self._lock:
            self._save()


class DynamoDurable:
    """Rows `DUR#<kind>#<key>` in the state table (with a TTL so stale closures and notices clean themselves up);
    robot status goes to its own table, keyed by robot id."""

    def __init__(self, client, names: dict | None = None):
        from .aws_dynamo import tables_from_env
        self.c, self.t = client, names or tables_from_env()

    @classmethod
    def from_env(cls) -> "DynamoDurable":
        import boto3
        return cls(boto3.client("dynamodb", region_name=os.environ.get("AWS_REGION", "ap-south-1")))

    def put(self, kind: str, key: str, value: dict, ttl_s: int | None = None) -> None:
        item = {"pk": {"S": f"DUR#{kind}#{key}"}, "kind": {"S": kind}, "key": {"S": key}, "value": {"S": json.dumps(value)}}
        if ttl_s:
            item["ttl"] = {"N": str(int(time.time()) + ttl_s)}
        self.c.put_item(TableName=self.t["state"], Item=item)

    def delete(self, kind: str, key: str) -> None:
        self.c.delete_item(TableName=self.t["state"], Key={"pk": {"S": f"DUR#{kind}#{key}"}})

    def load(self, kind: str) -> dict[str, dict]:
        out: dict[str, dict] = {}
        kw = {"TableName": self.t["state"], "ConsistentRead": True, "FilterExpression": "begins_with(pk, :p)",
              "ExpressionAttributeValues": {":p": {"S": f"DUR#{kind}#"}}}
        while True:
            page = self.c.scan(**kw)
            for it in page.get("Items", []):
                if "ttl" in it and int(it["ttl"]["N"]) < time.time():
                    continue                 # DynamoDB removes expired rows lazily; ignore them meanwhile
                out[it["key"]["S"]] = json.loads(it["value"]["S"])
            if "LastEvaluatedKey" not in page:
                return out
            kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def put_robot(self, robot_id: str, rec: dict) -> None:
        self.c.put_item(TableName=self.t["robot_status"], Item={
            "robot_id": {"S": robot_id}, **{k: {"S": json.dumps(v)} for k, v in rec.items()}})
