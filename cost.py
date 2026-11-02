"""Cost hooks: counters for every term of the PRD Sec. 7 per-task formula.

Cost = [T_in*p_in + T_out*p_out + F*p_fargate + M*p_iot + W*p_ddb + Q*p_sqs
        + S*p_s3 + D*p_transfer + G*p_apigw + K*p_cw] / N_tasks

This module only RECORDS the raw counters (T_in, T_out, F, M, W, Q, S, D, G,
K); prices stay symbolic per the PRD, and the division by N_tasks happens in
the evaluation tooling. No Cost Explorer calls. Values recorded by the local
API are stand-ins (e.g. tokens from a len//4 heuristic); Person 3's real
orchestrator records the same counters from Bedrock usage fields.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger("nimbus.cost")

_LOCK = threading.Lock()

COUNTERS = (
    "input_tokens",       # T_in
    "output_tokens",      # T_out
    "fargate_gbs",        # F, GB-seconds of always-on container time
    "iot_messages",       # M, leases/check-ins/escalations via IoT Core
    "dynamodb_ops",       # W, table reads/writes
    "sqs_messages",       # Q
    "s3_ops",             # S
    "data_transfer_bytes",  # D, inter-AZ/NAT bytes
    "apigw_requests",     # G
    "cloudwatch_metrics", # K, metrics/alarms/log volume
)


@runtime_checkable
class CostBackend(Protocol):
    """Protocol for cost counter storage engines (local file or DynamoDB)."""

    def record(self, **counts: int | float) -> None:
        """Add to named counters; unknown counter names are a coding error."""
        ...

    def snapshot(self) -> dict[str, int | float]:
        """Return a dictionary of all tracked counters."""
        ...


class LocalFileCostBackend:
    """Default local JSON file cost counter persistence (data/cost.json)."""

    def __init__(self, data_dir: Path | str):
        self.path = Path(data_dir) / "cost.json"
        self.counters: dict[str, int | float] = dict.fromkeys(COUNTERS, 0)
        if self.path.exists():
            try:
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                self.counters.update({k: saved[k] for k in COUNTERS if k in saved})
            except Exception:
                pass

    def record(self, **counts: int | float) -> None:
        with _LOCK:
            for name, amount in counts.items():
                if name not in self.counters:
                    raise KeyError(f"unknown cost counter {name!r}")
                self.counters[name] += amount
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.counters, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)

    def snapshot(self) -> dict[str, int | float]:
        with _LOCK:
            return dict(self.counters)


class DynamoCostBackend:
    """DynamoDB-backed cost counter persistence using atomic ADD updates."""

    def __init__(self, client: Any = None, table_name: str | None = None):
        self._client = client
        self._table_name = table_name
        self._lock = threading.Lock()
        self._local_cache = dict.fromkeys(COUNTERS, 0)

    @classmethod
    def from_env(cls) -> "DynamoCostBackend":
        import boto3
        region = os.environ.get("AWS_REGION", "ap-south-1")
        prefix = os.environ.get("NIMBUS_DDB_PREFIX", "nimbus-ds252")
        table = os.environ.get("NIMBUS_DDB_STATE_TABLE", f"{prefix}-state")
        client = boto3.client("dynamodb", region_name=region)
        return cls(client=client, table_name=table)

    def record(self, **counts: int | float) -> None:
        for name in counts:
            if name not in COUNTERS:
                raise KeyError(f"unknown cost counter {name!r}")
        with self._lock:
            for name, amount in counts.items():
                self._local_cache[name] += amount

        if self._client and self._table_name and counts:
            update_parts = []
            expr_names = {}
            expr_vals = {}
            for i, (name, amount) in enumerate(counts.items()):
                n_key = f"#c{i}"
                v_key = f":v{i}"
                update_parts.append(f"{n_key} {v_key}")
                expr_names[n_key] = name
                expr_vals[v_key] = {"N": str(amount)}
            try:
                self._client.update_item(
                    TableName=self._table_name,
                    Key={"pk": {"S": "COST#counters"}},
                    UpdateExpression="ADD " + ", ".join(update_parts),
                    ExpressionAttributeNames=expr_names,
                    ExpressionAttributeValues=expr_vals,
                )
            except Exception as exc:
                logger.warning("DynamoDB update_item failed; counts tracked in memory only: %s", exc)

    def snapshot(self) -> dict[str, int | float]:
        if self._client and self._table_name:
            try:
                res = self._client.get_item(
                    TableName=self._table_name,
                    Key={"pk": {"S": "COST#counters"}},
                    ConsistentRead=True,
                )
                item = res.get("Item", {})
                res_counters: dict[str, int | float] = dict.fromkeys(COUNTERS, 0)
                for k in COUNTERS:
                    if k in item and "N" in item[k]:
                        val_str = item[k]["N"]
                        try:
                            d = Decimal(str(val_str))
                            res_counters[k] = int(d) if d % 1 == 0 else float(d)
                        except (InvalidOperation, ValueError, TypeError) as exc:
                            logger.warning("Failed parsing counter %s value %r: %s", k, val_str, exc)
                            res_counters[k] = self._local_cache.get(k, 0)
                    elif k in item and isinstance(item[k], (int, float, Decimal)):
                        val = item[k]
                        if isinstance(val, Decimal):
                            res_counters[k] = int(val) if val % 1 == 0 else float(val)
                        elif isinstance(val, float) and val.is_integer():
                            res_counters[k] = int(val)
                        else:
                            res_counters[k] = val
                    else:
                        res_counters[k] = self._local_cache.get(k, 0)
                return res_counters
            except Exception as exc:
                logger.warning("DynamoDB get_item failed; falling back to local cache: %s", exc)
        with self._lock:
            return dict(self._local_cache)


def make_cost_backend(data_dir: Path | str | None = None) -> CostBackend:
    """Factory creating the appropriate cost backend from environment variables.

    DynamoDB is selected only when ``NIMBUS_COST_BACKEND`` is explicitly set to
    ``dynamodb`` (or the alias ``aws``). Setting ``NIMBUS_BACKEND=aws`` alone
    does **not** activate the DynamoDB cost backend.

    If ``NIMBUS_COST_BACKEND`` is unset, empty, or set to ``local`` / ``file``,
    the default local JSON file backend is used.
    Unknown non-empty values raise ValueError to prevent silent misconfiguration.
    """
    cost_backend_flag = os.environ.get("NIMBUS_COST_BACKEND", "").lower().strip()

    if not cost_backend_flag or cost_backend_flag in ("local", "file", "json"):
        return LocalFileCostBackend(data_dir or Path("data"))

    if cost_backend_flag in ("dynamodb", "aws"):
        try:
            return DynamoCostBackend.from_env()
        except Exception as exc:
            logger.warning(
                "DynamoDB cost backend requested but could not be initialised "
                "(%s); falling back to local file.", exc
            )
            return LocalFileCostBackend(data_dir or Path("data"))

    raise ValueError(
        f"Unknown NIMBUS_COST_BACKEND: {cost_backend_flag!r}. "
        "Valid options are 'local' (default) or 'dynamodb' (alias 'aws')."
    )


class CostBook:
    """Facade for tracking PRD Sec. 7 cost counters across swappable backends."""

    def __init__(self, data_dir: Path | str | None = None, backend: CostBackend | None = None):
        if backend is not None:
            self.backend = backend
        else:
            self.backend = make_cost_backend(data_dir)

    def record(self, **counts: int | float) -> None:
        """Add to named counters; unknown counter names are a coding error."""
        self.backend.record(**counts)

    def snapshot(self) -> dict[str, int | float]:
        """Return snapshot of all tracked counters."""
        return self.backend.snapshot()

    @property
    def path(self) -> Path | None:
        """Compatibility property for callers expecting local file path."""
        return getattr(self.backend, "path", None)

    @property
    def counters(self) -> dict[str, int]:
        """Compatibility property for callers inspecting internal counter state."""
        if hasattr(self.backend, "counters"):
            return self.backend.counters
        return self.backend.snapshot()
