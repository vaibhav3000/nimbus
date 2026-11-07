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
    "notice": "notice.schema.json",
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


def _parse_iso8601(value: object, field_name: str) -> tuple[datetime | None, str | None]:
    if value is None:
        return None, f"$.{field_name}: field is required"
    if not isinstance(value, str):
        return None, f"$.{field_name}: must be an ISO-8601 string, got {type(value).__name__}"
    try:
        normalized = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
    except Exception as exc:
        return None, f"$.{field_name}: invalid ISO-8601 timestamp '{value}' ({exc})"
    if dt.tzinfo is None:
        return None, f"$.{field_name}: timestamp '{value}' must be offset-aware (e.g. UTC 'Z' or offset)"
    return dt, None


def semantic_lease_errors(lease: dict) -> list[str]:
    """Cross-field lease rules that JSON Schema cannot express."""
    if not isinstance(lease, dict):
        return ["$: lease must be a JSON object"]

    errors: list[str] = []

    if "deadline_ts" not in lease:
        errors.append("$.deadline_ts: field is required in lease")
        deadline_dt = None
    else:
        deadline_dt, err = _parse_iso8601(lease["deadline_ts"], "deadline_ts")
        if err:
            errors.append(err)

    if "expiry_ts" not in lease:
        errors.append("$.expiry_ts: field is required in lease")
        expiry_dt = None
    else:
        expiry_dt, err = _parse_iso8601(lease["expiry_ts"], "expiry_ts")
        if err:
            errors.append(err)

    if deadline_dt is not None and expiry_dt is not None:
        if deadline_dt > expiry_dt:
            errors.append(
                f"deadline_ts ({lease['deadline_ts']}) must be <= expiry_ts "
                f"({lease['expiry_ts']}); the deadline must be reachable inside "
                "the leased authority window (SC-1)"
            )

    return errors
