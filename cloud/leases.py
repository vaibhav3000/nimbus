"""Lease Manager storage: atomic, conditional commits (PRD D4, FR-14/FR-6).

The interface is what DynamoDB implements with conditional writes plus
TransactWriteItems (cloud/aws_dynamo.py); MemoryLeaseRepo is the same
semantics under a lock for L0 and tests.

Invariants it enforces:
* one live holder per robot (and per *exclusive* zone such as a gate);
* a task's lease version is strictly +1 from its live version, starting at 1;
* a claim is live until the lease's expiry, so a crashed holder frees itself.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Protocol

from .clock import iso, parse_iso


class Conflict(Exception):
    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


class LeaseRepo(Protocol):
    def commit(self, lease: dict, claim_keys: list[str], *, now: datetime) -> None: ...
    def renew(self, task_id: str, version: int, new_expiry: str, *, now: datetime) -> dict: ...
    def release(self, task_id: str) -> list[str]: ...
    def holder(self, key: str, now: datetime) -> dict | None: ...
    def current(self, task_id: str) -> dict | None: ...


class MemoryLeaseRepo:
    def __init__(self, path: Path | None = None):
        self._lock = threading.RLock()
        self._path = Path(path) if path else None
        self.leases: dict[str, dict] = {}   # task_id -> {"lease": {...}, "state": "live"|"released"}
        self.claims: dict[str, dict] = {}   # key -> {"task_id","version","until"}
        if self._path and self._path.exists():
            blob = json.loads(self._path.read_text(encoding="utf-8"))
            self.leases, self.claims = blob["leases"], blob["claims"]

    def _save(self) -> None:
        if not self._path:
            return
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"leases": self.leases, "claims": self.claims}, indent=1), encoding="utf-8")
        os.replace(tmp, self._path)

    def holder(self, key: str, now: datetime) -> dict | None:
        with self._lock:
            c = self.claims.get(key)
            return c if c and parse_iso(c["until"]) > now else None

    def current(self, task_id: str) -> dict | None:
        with self._lock:
            rec = self.leases.get(task_id)
            return dict(rec["lease"]) if rec and rec["state"] == "live" else None

    def commit(self, lease: dict, claim_keys: list[str], *, now: datetime) -> None:
        task_id = _task_of(lease)
        with self._lock:
            rec = self.leases.get(task_id)
            live_version = rec["lease"]["version"] if rec and rec["state"] == "live" else None
            expected = 1 if live_version is None else live_version + 1
            if rec and rec["state"] == "released":
                raise Conflict([f"task {task_id} lease already released"])
            reasons = []
            if lease["version"] != expected:
                reasons.append(f"version {lease['version']} != expected {expected}")
            for key in claim_keys:
                held = self.holder(key, now)
                if held and held["task_id"] != task_id:
                    reasons.append(f"{key} held by {held['task_id']} v{held['version']}")
            if reasons:
                raise Conflict(reasons)
            for key, c in list(self.claims.items()):
                if c["task_id"] == task_id and key not in claim_keys:
                    del self.claims[key]
            for key in claim_keys:
                self.claims[key] = {"task_id": task_id, "version": lease["version"], "until": lease["expiry_ts"]}
            self.leases[task_id] = {"lease": dict(lease), "state": "live"}
            self._save()

    def renew(self, task_id: str, version: int, new_expiry: str, *, now: datetime) -> dict:
        with self._lock:
            rec = self.leases.get(task_id)
            if not rec or rec["state"] != "live":
                raise Conflict([f"no live lease for {task_id}"])
            lease = rec["lease"]
            if lease["version"] != version:
                raise Conflict([f"version {version} is not current ({lease['version']})"])
            if parse_iso(new_expiry) <= parse_iso(lease["expiry_ts"]):
                return dict(lease)  # never shorten authority by renewing
            lease["expiry_ts"] = new_expiry
            for c in self.claims.values():
                if c["task_id"] == task_id:
                    c["until"] = new_expiry
            self._save()
            return dict(lease)

    def release(self, task_id: str) -> list[str]:
        with self._lock:
            freed = [k for k, c in self.claims.items() if c["task_id"] == task_id]
            for k in freed:
                del self.claims[k]
            if task_id in self.leases:
                self.leases[task_id]["state"] = "released"
            self._save()
            return freed


def _task_of(lease: dict) -> str:
    # lease_id format fixed by P4: lease-<task_id>-v<N>
    return lease["lease_id"][len("lease-"): lease["lease_id"].rindex("-v")]


def claim_keys_for(lease: dict, exclusive_zones: set[str]) -> list[str]:
    keys = [f"robot:{r}" for r in lease["assigned_robots"]]
    keys += [f"zone:{z}" for z in lease["allowed_zones"] if z in exclusive_zones]
    return keys


def expiry_for(deadline_ts: str, grace_s: int = 180) -> str:
    from datetime import timedelta
    return iso(parse_iso(deadline_ts) + timedelta(seconds=grace_s))
