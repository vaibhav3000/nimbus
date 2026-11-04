"""Unit tests for pluggable cost backends in cost.py."""

from decimal import Decimal
import json
from pathlib import Path
import pytest

from cost import (
    COUNTERS,
    CostBook,
    DynamoCostBackend,
    LocalFileCostBackend,
    make_cost_backend,
    calculate_cost_per_task,
    CostCalculationResult,
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
            delta = Decimal(ExpressionAttributeValues[v_ref]["N"])
            curr = Decimal(item.get(attr_name, {}).get("N", "0"))
            res = curr + delta
            item[attr_name] = {"N": str(int(res)) if res % 1 == 0 else str(res)}
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


def test_dynamo_backend_float_fargate_gbs_roundtrip():
    """fargate_gbs is a float (e.g. 0.001). DynamoCostBackend must not truncate it via int()."""
    fake_client = FakeDynamoClient()
    table_name = "test-nimbus-float"

    # Writer instance records a small float
    writer = DynamoCostBackend(client=fake_client, table_name=table_name)
    writer.record(fargate_gbs=0.001)

    # Reader instance reads it back (simulates a second instance / fresh snapshot)
    reader = DynamoCostBackend(client=fake_client, table_name=table_name)
    snap = reader.snapshot()

    # Must preserve float precision, not be truncated to 0
    assert snap["fargate_gbs"] == pytest.approx(0.001, rel=1e-6), (
        f"Expected ~0.001 but got {snap['fargate_gbs']} — int() truncation bug"
    )

    # Multiple writes accumulate correctly
    writer.record(fargate_gbs=0.002)
    snap2 = reader.snapshot()
    assert snap2["fargate_gbs"] == pytest.approx(0.003, rel=1e-6)


def test_make_cost_backend_nimbus_backend_aws_does_not_activate_dynamo(
    tmp_path: Path, monkeypatch
):
    """NIMBUS_BACKEND=aws must NOT silently switch the cost backend to DynamoDB.
    Only NIMBUS_COST_BACKEND=dynamodb should do that."""
    monkeypatch.setenv("NIMBUS_BACKEND", "aws")
    monkeypatch.delenv("NIMBUS_COST_BACKEND", raising=False)
    b = make_cost_backend(tmp_path)
    assert isinstance(b, LocalFileCostBackend), (
        "NIMBUS_BACKEND=aws alone must not activate DynamoDB cost backend"
    )


def test_make_cost_backend_unknown_value_raises(tmp_path: Path, monkeypatch):
    """Unknown NIMBUS_COST_BACKEND value must raise ValueError rather than silently falling back."""
    monkeypatch.setenv("NIMBUS_COST_BACKEND", "unsupported_engine")
    with pytest.raises(ValueError, match="Unknown NIMBUS_COST_BACKEND"):
        make_cost_backend(tmp_path)


def test_calculate_cost_per_task_all_ten_terms():
    """Verify that calculate_cost_per_task accurately computes the 10-term PRD Sec 7 formula."""
    counters = {
        "input_tokens": 1000,         # T_in
        "output_tokens": 200,         # T_out
        "fargate_gbs": 50.0,          # F
        "iot_messages": 30,           # M
        "dynamodb_ops": 20,           # W
        "sqs_messages": 10,           # Q
        "s3_ops": 5,                  # S
        "data_transfer_bytes": 1024,  # D
        "apigw_requests": 15,         # G
        "cloudwatch_metrics": 8,      # K
    }

    # Pass symbolic pricing table using PRD p_* notation
    pricing = {
        "p_in": 0.000003,       # 1000 * 0.000003 = 0.003
        "p_out": 0.000015,      # 200 * 0.000015 = 0.003
        "p_fargate": 0.00005,   # 50 * 0.00005 = 0.0025
        "p_iot": 0.000001,      # 30 * 0.000001 = 0.00003
        "p_ddb": 0.00000125,    # 20 * 0.00000125 = 0.000025
        "p_sqs": 0.0000004,     # 10 * 0.0000004 = 0.000004
        "p_s3": 0.000005,       # 5 * 0.000005 = 0.000025
        "p_transfer": 0.00000009, # 1024 * 0.00000009 = 0.00009216
        "p_apigw": 0.000001,    # 15 * 0.000001 = 0.000015
        "p_cw": 0.0003,         # 8 * 0.0003 = 0.0024
    }

    num_tasks = 5
    result = calculate_cost_per_task(counters, pricing, num_tasks=num_tasks)

    # Check return type and representations
    assert isinstance(result, CostCalculationResult)
    assert isinstance(result, tuple)

    # Test tuple unpacking: breakdown, cost_per_task
    breakdown, cost_per_task = result
    assert isinstance(breakdown, dict)
    assert len(breakdown) == 10

    # Verify each individual term cost
    assert breakdown["input_tokens"] == pytest.approx(0.003)
    assert breakdown["output_tokens"] == pytest.approx(0.003)
    assert breakdown["fargate_gbs"] == pytest.approx(0.0025)
    assert breakdown["iot_messages"] == pytest.approx(0.00003)
    assert breakdown["dynamodb_ops"] == pytest.approx(0.000025)
    assert breakdown["sqs_messages"] == pytest.approx(0.000004)
    assert breakdown["s3_ops"] == pytest.approx(0.000025)
    assert breakdown["data_transfer_bytes"] == pytest.approx(0.00009216)
    assert breakdown["apigw_requests"] == pytest.approx(0.000015)
    assert breakdown["cloudwatch_metrics"] == pytest.approx(0.0024)

    expected_total = sum(breakdown.values())
    expected_cpt = expected_total / num_tasks

    assert result.total_cost == pytest.approx(expected_total)
    assert cost_per_task == pytest.approx(expected_cpt)
    assert result.cost_per_task == pytest.approx(expected_cpt)

    # Test dict-like key indexing
    assert result["breakdown"] == breakdown
    assert result["cost_per_task"] == pytest.approx(expected_cpt)
    assert result["total_cost"] == pytest.approx(expected_total)


def test_calculate_cost_per_task_zero_tasks_safe():
    """Verify that num_tasks = 0 is handled safely without ZeroDivisionError."""
    counters = {"input_tokens": 500, "output_tokens": 100}
    pricing = {"p_in": 0.000002, "p_out": 0.00001}

    # Zero tasks
    res_zero = calculate_cost_per_task(counters, pricing, num_tasks=0)
    assert res_zero.cost_per_task == 0.0
    assert res_zero["cost_per_task"] == 0.0
    assert res_zero.total_cost == pytest.approx(500 * 0.000002 + 100 * 0.00001)

    # Negative tasks (defensive)
    res_neg = calculate_cost_per_task(counters, pricing, num_tasks=-1)
    assert res_neg.cost_per_task == 0.0


def test_calculate_cost_per_task_pricing_aliases():
    """Pricing table can use counter names or short p_* aliases seamlessly."""
    counters = {"fargate_gbs": 10.0, "sqs_messages": 50}
    # Using counter names instead of p_*
    pricing = {"fargate_gbs": 0.0001, "sqs_messages": 0.00005}

    breakdown, cpt = calculate_cost_per_task(counters, pricing, num_tasks=2)
    assert breakdown["fargate_gbs"] == pytest.approx(0.001)
    assert breakdown["sqs_messages"] == pytest.approx(0.0025)
    assert cpt == pytest.approx((0.001 + 0.0025) / 2)


def test_calculate_cost_per_task_with_cost_book_integration(tmp_path: Path):
    """Confirm CostBook.snapshot() integrates directly and existing CostBook behavior is unchanged."""
    book = CostBook(tmp_path)
    book.record(input_tokens=2000, output_tokens=500, apigw_requests=4)

    # Verify existing CostBook methods behave as before
    snap = book.snapshot()
    assert snap["input_tokens"] == 2000
    assert snap["output_tokens"] == 500
    assert snap["apigw_requests"] == 4

    # Pass CostBook instance directly to calculate_cost_per_task
    pricing = {"p_in": 0.000001, "p_out": 0.000002, "p_apigw": 0.0001}
    res = calculate_cost_per_task(book, pricing, num_tasks=4)
    expected_total = (2000 * 0.000001) + (500 * 0.000002) + (4 * 0.0001)
    assert res.total_cost == pytest.approx(expected_total)
    assert res.cost_per_task == pytest.approx(expected_total / 4)
