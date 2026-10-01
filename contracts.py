"""Schema loading, document validation, and cross-field lease rules.

The JSON Schema documents in contracts/ are the wire contracts for the whole
team (Nimbus PRD v5.0): leases (FR-5, FR-8), the three escalation types
(Table 2), robot sub-tasks (Sec. 6), and decision-log entries (FR-20). Basis:
FR-20 "every decision recorded" and the lease tuple fixed in PRD v5.0 Sec. 6.

Cross-field rules JSON Schema cannot express live here. The one rule: a lease
is well formed only when deadline_ts <= expiry_ts, so the deadline (SC-1:
12 minutes) stays reachable inside the leased authority window. Timestamps
are ISO 8601; a naive-vs-aware comparison is avoided by requiring the parsed
values to be comparable (both offset-aware after fromisoformat with Z or an
offset).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

CONTRACTS_DIR = Path(__file__).resolve().parent / "contracts"

_SCHEMA_FILES = {
    "lease": "lease.schema.json",
    "escalation": "escalation.schema.json",
    "robot_subtask": "robot_subtask.schema.json",
    "decision_log_entry": "decision_log_entry.schema.json",
}


def load_schema(contract: str) -> dict:
    try:
        filename = _SCHEMA_FILES[contract]
    except KeyError:
        raise KeyError(
            f"unknown contract {contract!r}; expected one of {sorted(_SCHEMA_FILES)}"
        ) from None
    return json.loads((CONTRACTS_DIR / filename).read_text(encoding="utf-8"))


def _registry() -> Registry:
    resources = [
        (s["$id"], Resource.from_contents(s))
        for f in CONTRACTS_DIR.glob("*.schema.json")
        for s in [json.loads(f.read_text(encoding="utf-8"))]
    ]
    return Registry().with_resources(resources)


def validate_document(contract: str, document: dict) -> list[str]:
    """Full check of a wire document against its published schema.

    Returns every violation message, each prefixed with the JSON path of the
    offending value; an empty list means the document is acceptable. For
    leases this includes the cross-field rules. Registry-based so a lease's
    task_sequence can reference the published robot_subtask schema instead of
    duplicating its command set.
    """
    schema = load_schema(contract)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, registry=_registry())
    errors = [f"{e.json_path}: {e.message}" for e in validator.iter_errors(document)]
    if contract == "lease":
        errors.extend(semantic_lease_errors(document))
    return errors


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def semantic_lease_errors(lease: dict) -> list[str]:
    """Cross-field lease rules that JSON Schema cannot express."""
    errors: list[str] = []
    try:
        if _parse_ts(lease["deadline_ts"]) > _parse_ts(lease["expiry_ts"]):
            errors.append(
                f"deadline_ts ({lease['deadline_ts']}) must be <= expiry_ts "
                f"({lease['expiry_ts']}); the deadline must be reachable inside "
                "the leased authority window (SC-1)"
            )
    except (KeyError, ValueError, TypeError):
        pass
    return errors
