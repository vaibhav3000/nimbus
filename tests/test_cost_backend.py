"""Unit tests for pluggable cost backends in cost.py."""

import json
from pathlib import Path
import pytest

from cost import (
    COUNTERS,
    CostBook,
    DynamoCostBackend,
    LocalFileCostBackend,
    make_cost_backend,
)


class FakeDynamoClient:
    """In-memory mock for DynamoDB table operations used by DynamoCostBackend."""

    def __init__(self):
        self.tables: dict[str, dict[str, dict]] = {}

    def update_item(
        self,
        TableName: str,
        Key: dict,
        UpdateExpression: str,
        ExpressionAttributeNames: dict,
        ExpressionAttributeValues: dict,
    ):
        table = self.tables.setdefault(TableName, {})
        pk = Key["pk"]["S"]
        item = table.setdefault(pk, {"pk": {"S": pk}})

        # Parse simple "ADD #c0 :v0, #c1 :v1"
        assert UpdateExpression.startswith("ADD ")
        clauses = UpdateExpression[4:].split(", ")
        for clause in clauses:
            n_ref, v_ref = clause.split(" ")
            attr_name = ExpressionAttributeNames[n_ref]
            delta = int(ExpressionAttributeValues[v_ref]["N"])
            curr = int(item.get(attr_name, {}).get("N", "0"))
            item[attr_name] = {"N": str(curr + delta)}
        return {"Attributes": item}

    def get_item(self, TableName: str, Key: dict, ConsistentRead: bool = False):
        table = self.tables.get(TableName, {})
        pk = Key["pk"]["S"]
        item = table.get(pk)
        return {"Item": item} if item else {}


def test_local_file_backend_records_and_persists(tmp_path: Path):
    backend = LocalFileCostBackend(tmp_path)
    cost_file = tmp_path / "cost.json"

    # Initial state should be all zeros
    snap0 = backend.snapshot()
    for c in COUNTERS:
        assert snap0[c] == 0

    # Record some counters
    backend.record(apigw_requests=2, input_tokens=150, iot_messages=3)
    snap1 = backend.snapshot()
    assert snap1["apigw_requests"] == 2
    assert snap1["input_tokens"] == 150
    assert snap1["iot_messages"] == 3
    assert snap1["output_tokens"] == 0

    # Verify persisted JSON on disk
    assert cost_file.exists()
    disk_data = json.loads(cost_file.read_text(encoding="utf-8"))
    assert disk_data["apigw_requests"] == 2
    assert disk_data["input_tokens"] == 150
    assert disk_data["iot_messages"] == 3

    # Reopening from same directory restores counts
    reloaded = LocalFileCostBackend(tmp_path)
    assert reloaded.snapshot()["input_tokens"] == 150


def test_local_file_backend_rejects_unknown_counter(tmp_path: Path):
    backend = LocalFileCostBackend(tmp_path)
    with pytest.raises(KeyError, match="unknown cost counter 'non_existent_cost'"):
        backend.record(non_existent_cost=10)


def test_dynamo_backend_with_mock_client():
    fake_client = FakeDynamoClient()
    table_name = "test-nimbus-state"
    backend = DynamoCostBackend(client=fake_client, table_name=table_name)

    # Initially empty snapshot
    snap0 = backend.snapshot()
    for c in COUNTERS:
        assert snap0[c] == 0

    # Record counters
    backend.record(iot_messages=5, dynamodb_ops=12, fargate_gbs=4)
    snap1 = backend.snapshot()
    assert snap1["iot_messages"] == 5
    assert snap1["dynamodb_ops"] == 12
    assert snap1["fargate_gbs"] == 4
    assert snap1["input_tokens"] == 0

    # Verify stored representation in fake DynamoDB
    item = fake_client.tables[table_name]["COST#counters"]
    assert item["iot_messages"]["N"] == "5"
    assert item["dynamodb_ops"]["N"] == "12"
    assert item["fargate_gbs"]["N"] == "4"

    # Incremental update adds correctly
    backend.record(iot_messages=2)
    snap2 = backend.snapshot()
    assert snap2["iot_messages"] == 7


def test_dynamo_backend_rejects_unknown_counter():
    fake_client = FakeDynamoClient()
    backend = DynamoCostBackend(client=fake_client, table_name="test-table")
    with pytest.raises(KeyError, match="unknown cost counter 'bogus'"):
        backend.record(bogus=1)


def test_make_cost_backend_flag_selection(tmp_path: Path, monkeypatch):
    # Default (no flags) -> LocalFileCostBackend
    monkeypatch.delenv("NIMBUS_COST_BACKEND", raising=False)
    monkeypatch.delenv("NIMBUS_BACKEND", raising=False)
    b_default = make_cost_backend(tmp_path)
    assert isinstance(b_default, LocalFileCostBackend)
    assert b_default.path == tmp_path / "cost.json"

    # Explicit NIMBUS_COST_BACKEND=local
    monkeypatch.setenv("NIMBUS_COST_BACKEND", "local")
    b_local = make_cost_backend(tmp_path)
    assert isinstance(b_local, LocalFileCostBackend)

    # Explicit NIMBUS_COST_BACKEND=dynamodb without AWS creds falls back safely
    monkeypatch.setenv("NIMBUS_COST_BACKEND", "dynamodb")
    b_ddb = make_cost_backend(tmp_path)
    # Since boto3 is installed, DynamoCostBackend.from_env() succeeds or falls back safely
    assert isinstance(b_ddb, (DynamoCostBackend, LocalFileCostBackend))


def test_cost_book_facade_compatibility(tmp_path: Path):
    book = CostBook(tmp_path)
    assert isinstance(book.backend, LocalFileCostBackend)
    assert book.path == tmp_path / "cost.json"

    # Test record and snapshot
    book.record(apigw_requests=1, data_transfer_bytes=512)
    s = book.snapshot()
    assert s["apigw_requests"] == 1
    assert s["data_transfer_bytes"] == 512
    assert book.counters["apigw_requests"] == 1
