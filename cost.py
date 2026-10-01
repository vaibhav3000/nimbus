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

import json
import os
import threading
from pathlib import Path

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


class CostBook:
    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "cost.json"
        self.counters = dict.fromkeys(COUNTERS, 0)
        if self.path.exists():
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            self.counters.update({k: saved[k] for k in COUNTERS if k in saved})

    def record(self, **counts: int) -> None:
        """Add to named counters; unknown counter names are a coding error."""
        with _LOCK:
            for name, amount in counts.items():
                if name not in self.counters:
                    raise KeyError(f"unknown cost counter {name!r}")
                self.counters[name] += amount
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.counters, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)

    def snapshot(self) -> dict:
        with _LOCK:
            return dict(self.counters)
