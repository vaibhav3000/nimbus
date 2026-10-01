"""Contract tests: fixtures against the published JSON Schemas in contracts/.

These are the same documents Person 2 (edge) and Person 3 (cloud) will emit;
the only behaviour tested is schema validity plus the documented cross-field rule.
"""

import json
from pathlib import Path

import pytest

import contracts

CONTRACTS = Path(__file__).resolve().parent.parent / "contracts"


def raw_schema(name: str) -> dict:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


SC1_LEASE = {
    "lease_id": "lease-t1-0001",
    "version": 1,
    "priority": "urgent",
    "deadline_ts": "2026-10-02T10:12:00Z",
    "expiry_ts": "2026-10-02T10:15:00Z",
    "allowed_zones": ["2B"],
    "forbidden_zones": ["2C"],
    "speed_limit": 0.8,
    "assigned_robots": ["R2"],
    "cluster_id": "east-wing",
    "task_sequence": [{"cmd": "go_to", "robot": "R2", "params": {"to": "2B-12"}}],
    "when_to_escalate": ["door_closed", "deadline_at_risk", "robot_failed"],
}


def test_valid_sc1_lease_passes():
    assert contracts.validate_document("lease", SC1_LEASE) == []


def test_lease_rejects_missing_required_field():
    lease = {k: v for k, v in SC1_LEASE.items() if k != "speed_limit"}
    errors = contracts.validate_document("lease", lease)
    assert any("speed_limit" in e for e in errors)


def test_lease_rejects_extra_property():
    lease = dict(SC1_LEASE, waypoints=["A"])
    assert any("waypoints" in e for e in contracts.validate_document("lease", lease))


def test_lease_rejects_bad_version_and_priority():
    errors = contracts.validate_document("lease", dict(SC1_LEASE, version=0))
    assert any("version" in e for e in errors)
    errors = contracts.validate_document("lease", dict(SC1_LEASE, priority="whenever"))
    assert any("priority" in e for e in errors)


def test_lease_rejects_bad_cmd_in_task_sequence():
    lease = dict(SC1_LEASE, task_sequence=[{"cmd": "fly", "robot": "R2"}])
    assert contracts.validate_document("lease", lease)


def test_lease_rejects_deadline_after_expiry():
    lease = dict(SC1_LEASE, deadline_ts="2026-10-02T10:20:00Z")
    assert contracts.validate_document("lease", lease)


CLARIFY = {
    "type": "CLARIFY",
    "message_id": "msg-0007",
    "lease_version": 1,
    "task_id": "task-0001",
    "payload": {
        "question": "2B-12 is a paediatric bed. Do you need a paediatric pump?",
        "possible_readings": ["paediatric infusion pump", "standard infusion pump"],
    },
}

UNEXPECTED = {
    "type": "UNEXPECTED_EVENT",
    "message_id": "msg-0008",
    "lease_version": 2,
    "task_id": "task-0001",
    "payload": {
        "what_happened": "gate into West wing closed",
        "where": "west-wing gate G4",
        "robots_saw": ["R4 reports gate shut at 10:03"],
    },
}

OUTPUT_READY = {
    "type": "OUTPUT_READY",
    "message_id": "msg-0009",
    "lease_version": 1,
    "task_id": "task-0001",
    "payload": {"result_ref": "s3://nimbus-outputs/task-0001/photo-1.jpg"},
}


def test_all_three_escalation_types_pass():
    for esc in (CLARIFY, UNEXPECTED, OUTPUT_READY):
        assert contracts.validate_document("escalation", esc) == []


def test_escalation_rejects_unknown_type():
    esc = dict(OUTPUT_READY, type="STATUS_UPDATE")
    assert contracts.validate_document("escalation", esc)


def test_escalation_rejects_payload_missing_table2_keys():
    esc = {**CLARIFY, "payload": {"question": "…"}}  # no possible_readings
    assert contracts.validate_document("escalation", esc)
    esc = {**OUTPUT_READY, "payload": {"note": "done"}}  # no result_ref
    assert contracts.validate_document("escalation", esc)


def test_escalation_requires_message_id_and_lease_version():
    esc = {k: v for k, v in CLARIFY.items() if k != "message_id"}
    assert any("message_id" in e for e in contracts.validate_document("escalation", esc))
    esc = {k: v for k, v in CLARIFY.items() if k != "lease_version"}
    assert any(
        "lease_version" in e for e in contracts.validate_document("escalation", esc)
    )


DECISION_ROW = {
    "who": "cloud",
    "inputs": {"request_text": "Need an infusion pump at 2B-12, urgent."},
    "output": {"decision": "ask_clarify", "question": "Do you need a paediatric pump?"},
    "lease_version": None,
    "timestamp": "2026-10-02T10:00:05Z",
    "task_id": "task-0001",
}


def test_valid_decision_log_entry_passes():
    assert contracts.validate_document("decision_log_entry", DECISION_ROW) == []


def test_decision_log_entry_rejects_bad_who():
    row = dict(DECISION_ROW, who="robot")
    assert any("who" in e for e in contracts.validate_document("decision_log_entry", row))


def test_decision_log_entry_rejects_missing_fields():
    row = {k: v for k, v in DECISION_ROW.items() if k != "task_id"}
    errors = contracts.validate_document("decision_log_entry", row)
    assert any("task_id" in e for e in errors)


def test_valid_robot_subtask_standalone():
    for cmd in ("go_to", "wait", "stop", "photo", "beep", "report"):
        assert contracts.validate_document("robot_subtask", {"cmd": cmd}) == []
    assert contracts.validate_document("robot_subtask", {"cmd": "dock"})
