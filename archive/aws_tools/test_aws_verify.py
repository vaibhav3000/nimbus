"""The AWS verification script, exercised on emulated services (the real run is yours; see docs/aws-verify.md)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import aws_verify  # noqa: E402

moto = pytest.importorskip("moto")


def test_checks_pass_on_emulated_aws_and_skip_what_is_not_configured(monkeypatch):
    import boto3
    from cloud.aws_dynamo import ensure_tables, tables_from_env
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(k, "x")
    monkeypatch.setattr(aws_verify, "REGION", "ap-south-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-south-1")
    monkeypatch.setenv("NIMBUS_DDB_PREFIX", "nimbus-verify")
    monkeypatch.setenv("NIMBUS_RESULTS_BUCKET", "verify-bucket")
    for k in ("NIMBUS_BEDROCK_MODEL", "NIMBUS_IOT_ENDPOINT", "NIMBUS_KB_ID"):
        monkeypatch.delenv(k, raising=False)
    with moto.mock_aws():
        ensure_tables(boto3.client("dynamodb", region_name="ap-south-1"), tables_from_env())
        boto3.client("s3", region_name="ap-south-1").create_bucket(Bucket="verify-bucket", CreateBucketConfiguration={"LocationConstraint": "ap-south-1"})
        url = boto3.client("sqs", region_name="ap-south-1").create_queue(QueueName="q")["QueueUrl"]
        monkeypatch.setenv("NIMBUS_SQS_QUEUE_URL", url)
        lines = []
        res = aws_verify.run(boto3_module=boto3, out=lines.append)
    assert {k: v[0] for k, v in res.items()} == {"identity": "PASS", "bedrock": "SKIP", "dynamodb": "PASS", "s3": "PASS", "sqs": "PASS", "iot": "SKIP", "kb": "SKIP"}
    assert "set NIMBUS_BEDROCK_MODEL" in res["bedrock"][1] and "a foreign bucket is refused: True" in res["s3"][1]


def test_a_missing_table_is_a_clear_failure(monkeypatch):
    import boto3
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(k, "x")
    monkeypatch.setenv("NIMBUS_DDB_PREFIX", "nope")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-south-1")
    with moto.mock_aws():
        res = aws_verify.run(only=["dynamodb"], boto3_module=boto3, out=lambda s: None)
    assert res["dynamodb"][0] == "FAIL" and "tables not found" in res["dynamodb"][1]
