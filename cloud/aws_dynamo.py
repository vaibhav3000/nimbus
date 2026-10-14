"""DynamoDB implementation of the Lease Manager storage (PRD D4).

Same contract as MemoryLeaseRepo, enforced by DynamoDB itself:

  state table (pk):  TASK#<task>   {version, state, lease(json), claims[]}
                     ROBOT#<id> / ZONE#<id>  {task_id, version, until_epoch}
  leases table:      one immutable item per issued lease_id (audit history)

A commit is ONE TransactWriteItems call: the task pointer moves from version
v to v+1 only if it is still at v, every robot/exclusive-zone claim is taken
only if free, expired or already ours, and the history item is written. Either
all of that happens or none of it, so two clusters can never both hold a robot.
"""

from __future__ import annotations

import json
import os
from datetime import datetime

from .clock import parse_iso
from .leases import Conflict, _task_of

CONDITION_FAILED = "ConditionalCheckFailed"


def tables_from_env() -> dict:
    p = os.environ.get("NIMBUS_DDB_PREFIX", "nimbus-ds252")
    return {"state": f"{p}-state", "leases": f"{p}-leases", "tasks": f"{p}-tasks",
            "decision_log": f"{p}-decision_log"}


def ensure_tables(client, names: dict) -> None:
    """Create the tables the adapters need (dev/test only: Terraform owns them
    in a real deployment, see infra/terraform/p3.tf)."""
    spec = {
        names["state"]: ([("pk", "S")], None),
        names["leases"]: ([("lease_id", "S")], None),
        names["tasks"]: ([("task_id", "S")], None),
        names["decision_log"]: ([("task_id", "S"), ("ts", "S")], "ts"),
    }
    existing = set(client.list_tables()["TableNames"])
    for name, (attrs, range_key) in spec.items():
        if name in existing:
            continue
        schema = [{"AttributeName": attrs[0][0], "KeyType": "HASH"}]
        if range_key:
            schema.append({"AttributeName": range_key, "KeyType": "RANGE"})
        client.create_table(
            TableName=name, BillingMode="PAY_PER_REQUEST", KeySchema=schema,
            AttributeDefinitions=[{"AttributeName": a, "AttributeType": t} for a, t in attrs])
    try:
        client.update_time_to_live(TableName=names["state"],
                                  TimeToLiveSpecification={"Enabled": True, "AttributeName": "ttl"})
    except Exception:  # TTL is housekeeping; absence never affects correctness
        pass


def _s(v): return {"S": v}
def _n(v): return {"N": str(v)}


class DynamoLeaseRepo:
    def __init__(self, client, names: dict | None = None):
        self.c = client
        self.t = names or tables_from_env()

    @classmethod
    def from_env(cls) -> "DynamoLeaseRepo":
        import boto3
        return cls(boto3.client("dynamodb", region_name=os.environ.get("AWS_REGION", "ap-south-1")))

    # -- reads ------------------------------------------------------------------

    def _get(self, pk: str) -> dict | None:
        return self.c.get_item(TableName=self.t["state"], Key={"pk": _s(pk)}, ConsistentRead=True).get("Item")

    def holder(self, key: str, now: datetime) -> dict | None:
        kind, _, name = key.partition(":")
        item = self._get(f"{kind.upper()}#{name}")
        if not item or float(item["until_epoch"]["N"]) <= now.timestamp():
            return None
        return {"task_id": item["task_id"]["S"], "version": int(item["version"]["N"]),
                "until": item["until"]["S"]}

    def current(self, task_id: str) -> dict | None:
        item = self._get(f"TASK#{task_id}")
        if not item or item["state"]["S"] != "live":
            return None
        return json.loads(item["lease"]["S"])

    # -- writes -------------------------------------------------------------------

    def _claim_put(self, key: str, task_id: str, lease: dict, now: datetime) -> dict:
        kind, _, name = key.partition(":")
        until = parse_iso(lease["expiry_ts"])
        return {"Put": {
            "TableName": self.t["state"],
            "Item": {"pk": _s(f"{kind.upper()}#{name}"), "task_id": _s(task_id), "version": _n(lease["version"]),
                     "until": _s(lease["expiry_ts"]), "until_epoch": _n(until.timestamp()),
                     "ttl": _n(int(until.timestamp()) + 86400)},
            "ConditionExpression": "attribute_not_exists(pk) OR until_epoch <= :now OR task_id = :me",
            "ExpressionAttributeValues": {":now": _n(now.timestamp()), ":me": _s(task_id)}}}

    def commit(self, lease: dict, claim_keys: list[str], *, now: datetime) -> None:
        task_id = _task_of(lease)
        cur = self._get(f"TASK#{task_id}")
        if cur and cur["state"]["S"] == "released":
            raise Conflict([f"task {task_id} lease already released"])
        old_keys = [k["S"] for k in cur["claims"]["L"]] if cur else []
        expected = int(cur["version"]["N"]) + 1 if cur else 1
        if lease["version"] != expected:
            raise Conflict([f"version {lease['version']} != expected {expected}"])

        task_put = {"Put": {
            "TableName": self.t["state"],
            "Item": {"pk": _s(f"TASK#{task_id}"), "version": _n(lease["version"]), "state": _s("live"),
                     "lease": _s(json.dumps(lease)), "claims": {"L": [_s(k) for k in claim_keys]}},
            **({"ConditionExpression": "#v = :cur AND #s = :live",
                "ExpressionAttributeNames": {"#v": "version", "#s": "state"},
                "ExpressionAttributeValues": {":cur": _n(expected - 1), ":live": _s("live")}}
               if cur else {"ConditionExpression": "attribute_not_exists(pk)"})}}
        ops = [task_put] + [self._claim_put(k, task_id, lease, now) for k in claim_keys]
        for k in old_keys:
            if k not in claim_keys:
                kind, _, name = k.partition(":")
                ops.append({"Delete": {"TableName": self.t["state"], "Key": {"pk": _s(f"{kind.upper()}#{name}")},
                                       "ConditionExpression": "attribute_not_exists(pk) OR task_id = :me",
                                       "ExpressionAttributeValues": {":me": _s(task_id)}}})
        ops.append({"Put": {"TableName": self.t["leases"],
                            "Item": {"lease_id": _s(lease["lease_id"]), "task_id": _s(task_id),
                                     "version": _n(lease["version"]), "lease": _s(json.dumps(lease))},
                            "ConditionExpression": "attribute_not_exists(lease_id)"}})
        labels = ["task pointer"] + [f"{k}" for k in claim_keys] + [f"drop {k}" for k in old_keys if k not in claim_keys] + ["history"]
        self._transact(ops, labels)

    def renew(self, task_id: str, version: int, new_expiry: str, *, now: datetime) -> dict:
        cur = self._get(f"TASK#{task_id}")
        if not cur or cur["state"]["S"] != "live":
            raise Conflict([f"no live lease for {task_id}"])
        lease = json.loads(cur["lease"]["S"])
        if lease["version"] != version:
            raise Conflict([f"version {version} is not current ({lease['version']})"])
        if parse_iso(new_expiry) <= parse_iso(lease["expiry_ts"]):
            return lease
        lease["expiry_ts"] = new_expiry
        until = parse_iso(new_expiry).timestamp()
        keys = [k["S"] for k in cur["claims"]["L"]]
        ops = [{"Update": {"TableName": self.t["state"], "Key": {"pk": _s(f"TASK#{task_id}")},
                           "UpdateExpression": "SET #l = :l",
                           "ConditionExpression": "#v = :v AND #s = :live",
                           "ExpressionAttributeNames": {"#v": "version", "#s": "state", "#l": "lease"},
                           "ExpressionAttributeValues": {":l": _s(json.dumps(lease)), ":v": _n(version), ":live": _s("live")}}}]
        for k in keys:
            kind, _, name = k.partition(":")
            ops.append({"Update": {"TableName": self.t["state"], "Key": {"pk": _s(f"{kind.upper()}#{name}")},
                                   "UpdateExpression": "SET #u = :u, until_epoch = :e, #ttl = :t",
                                   "ConditionExpression": "task_id = :me",
                                   "ExpressionAttributeNames": {"#u": "until", "#ttl": "ttl"},
                                   "ExpressionAttributeValues": {":u": _s(new_expiry), ":e": _n(until),
                                                                 ":t": _n(int(until) + 86400), ":me": _s(task_id)}}})
        self._transact(ops, ["task pointer"] + keys)
        return lease

    def release(self, task_id: str) -> list[str]:
        cur = self._get(f"TASK#{task_id}")
        if not cur:
            return []
        keys = [k["S"] for k in cur["claims"]["L"]]
        ops = [{"Update": {"TableName": self.t["state"], "Key": {"pk": _s(f"TASK#{task_id}")},
                           "UpdateExpression": "SET #s = :r", "ExpressionAttributeNames": {"#s": "state"},
                           "ExpressionAttributeValues": {":r": _s("released")}}}]
        for k in keys:
            kind, _, name = k.partition(":")
            ops.append({"Delete": {"TableName": self.t["state"], "Key": {"pk": _s(f"{kind.upper()}#{name}")},
                                   "ConditionExpression": "attribute_not_exists(pk) OR task_id = :me",
                                   "ExpressionAttributeValues": {":me": _s(task_id)}}})
        self._transact(ops, ["task pointer"] + keys)
        return keys

    def _transact(self, ops: list, labels: list[str]) -> None:
        try:
            self.c.transact_write_items(TransactItems=ops)
        except self.c.exceptions.TransactionCanceledException as exc:
            reasons = exc.response.get("CancellationReasons", [])
            failed = [labels[i] for i, r in enumerate(reasons) if r.get("Code") == CONDITION_FAILED]
            if failed or reasons:
                raise Conflict([f"{f}: condition failed (held or version moved)" for f in failed]
                               or ["transaction cancelled"]) from exc
            raise
