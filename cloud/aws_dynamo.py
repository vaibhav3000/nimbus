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
            "decision_log": f"{p}-decision_log", "robot_status": f"{p}-robot_status"}


def ensure_tables(client, names: dict) -> None:
    """Create the tables the adapters need (dev/test only: Terraform owns them
    in a real deployment, see infra/terraform/p3.tf)."""
    spec = {
        names["state"]: ([("pk", "S")], None),
        names["leases"]: ([("lease_id", "S")], None),
        names["tasks"]: ([("task_id", "S")], None),
        names["decision_log"]: ([("task_id", "S"), ("ts", "S")], "ts"),
        names["robot_status"]: ([("robot_id", "S")], None),
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


STOCK_LABEL = "stock"


def _s(v): return {"S": v}
def _n(v): return {"N": str(v)}


def _stock_pk(cluster_id: str, item: str) -> str:
    return f"STOCK#{cluster_id}#{item}"


class DynamoStockStore:
    """Durable stock: one row per cluster and item in the state table
    (pk STOCK#<cluster>#<item>, attribute qty). Decrements happen inside the
    lease transaction (DynamoLeaseRepo.commit); this class seeds, loads and
    sets absolute counts, and gives an item back."""

    def __init__(self, client, names: dict | None = None):
        self.c = client
        self.t = names or tables_from_env()

    @classmethod
    def from_env(cls) -> "DynamoStockStore":
        import boto3
        return cls(boto3.client("dynamodb", region_name=os.environ.get("AWS_REGION", "ap-south-1")))

    def seed(self, clusters: dict) -> None:
        """Write the seed counts for rows that do not exist yet; existing rows are left alone."""
        for cid, c in clusters.items():
            for item, qty in c["stock"].items():
                try:
                    self.c.put_item(TableName=self.t["state"], ConditionExpression="attribute_not_exists(pk)",
                                    Item={"pk": _s(_stock_pk(cid, item)), "cluster": _s(cid), "item": _s(item), "qty": _n(qty)})
                except self.c.exceptions.ConditionalCheckFailedException:
                    pass

    def load(self) -> dict:
        """cluster -> {item: qty} for every stock row."""
        out: dict = {}
        kw = {"TableName": self.t["state"], "ConsistentRead": True,
              "FilterExpression": "begins_with(pk, :p)", "ExpressionAttributeValues": {":p": _s("STOCK#")}}
        while True:
            page = self.c.scan(**kw)
            for it in page.get("Items", []):
                out.setdefault(it["cluster"]["S"], {})[it["item"]["S"]] = int(it["qty"]["N"])
            if "LastEvaluatedKey" not in page:
                return out
            kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def set(self, cluster_id: str, item: str, qty: int) -> None:
        self.c.put_item(TableName=self.t["state"], Item={"pk": _s(_stock_pk(cluster_id, item)), "cluster": _s(cluster_id),
                                                         "item": _s(item), "qty": _n(qty)})

    def add(self, cluster_id: str, item: str, delta: int) -> None:
        self.c.update_item(TableName=self.t["state"], Key={"pk": _s(_stock_pk(cluster_id, item))},
                           UpdateExpression="SET qty = if_not_exists(qty, :z) + :d, #c = :c, #i = :i",
                           ExpressionAttributeNames={"#c": "cluster", "#i": "item"},
                           ExpressionAttributeValues={":z": _n(0), ":d": _n(delta), ":c": _s(cluster_id), ":i": _s(item)})


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

    def commit(self, lease: dict, claim_keys: list[str], *, now: datetime, stock: tuple | None = None) -> None:
        """`stock` = (cluster, item): take one from the durable count in the same
        transaction as the lease and the robot claim, so they succeed or fail together."""
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
        if stock:
            ops.append({"Update": {"TableName": self.t["state"], "Key": {"pk": _s(_stock_pk(*stock))},
                                   "UpdateExpression": "SET #q = #q - :one",
                                   "ConditionExpression": "attribute_exists(pk) AND #q >= :one",
                                   "ExpressionAttributeNames": {"#q": "qty"},
                                   "ExpressionAttributeValues": {":one": _n(1)}}})
            labels.append(STOCK_LABEL)
        self._transact(ops, labels)

    def last_lease(self, task_id: str) -> dict | None:
        cur = self._get(f"TASK#{task_id}")
        return json.loads(cur["lease"]["S"]) if cur else None

    def live_leases(self) -> list[dict]:
        out: list[dict] = []
        kw = {"TableName": self.t["state"], "ConsistentRead": True,
              "FilterExpression": "begins_with(pk, :p) AND #s = :live",
              "ExpressionAttributeNames": {"#s": "state"},
              "ExpressionAttributeValues": {":p": _s("TASK#"), ":live": _s("live")}}
        while True:
            page = self.c.scan(**kw)
            out += [json.loads(it["lease"]["S"]) for it in page.get("Items", [])]
            if "LastEvaluatedKey" not in page:
                return out
            kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]

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

    def free_claims(self, task_id: str) -> list[str]:
        cur = self._get(f"TASK#{task_id}")
        if not cur or cur["state"]["S"] != "live":
            return []
        keys = [k["S"] for k in cur["claims"]["L"]]
        ops = [{"Update": {"TableName": self.t["state"], "Key": {"pk": _s(f"TASK#{task_id}")},
                           "UpdateExpression": "SET claims = :none", "ConditionExpression": "#s = :live",
                           "ExpressionAttributeNames": {"#s": "state"},
                           "ExpressionAttributeValues": {":none": {"L": []}, ":live": _s("live")}}}]
        for k in keys:
            kind, _, name = k.partition(":")
            ops.append({"Delete": {"TableName": self.t["state"], "Key": {"pk": _s(f"{kind.upper()}#{name}")},
                                   "ConditionExpression": "attribute_not_exists(pk) OR task_id = :me",
                                   "ExpressionAttributeValues": {":me": _s(task_id)}}})
        self._transact(ops, ["task pointer"] + keys)
        return keys

    def release(self, task_id: str) -> list[str]:
        cur = self._get(f"TASK#{task_id}")
        if not cur or cur["state"]["S"] == "released":
            return []   # idempotent: the claims may already belong to another task
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
                raise Conflict([("stock taken concurrently" if f == STOCK_LABEL else
                                 f"{f}: condition failed (held or version moved)") for f in failed]
                               or ["transaction cancelled"]) from exc
            raise
