from __future__ import annotations

import copy

from experiments.robot_edge import r10_escalation_contracts as r10


def cases():
    return {case["case_id"]: case for case in r10.fixture_cases()}


def test_all_three_valid_escalations_use_published_validator():
    table = cases()
    for name in ("fixture-clarify-valid", "fixture-unexpected-valid", "fixture-output-valid"):
        ok, errors, path = r10.validate(table[name]["category"], table[name]["message"])
        assert ok, errors
        assert path == "contracts.validate_document(escalation)"


def test_negative_messages_are_rejected_and_oversize_is_detected():
    table = cases()
    for name in ("negative-clarify-missing-readings", "negative-unknown-escalation-type", "negative-invalid-escalation-identifiers", "negative-lease-missing-speed", "negative-checkin-missing-id"):
        ok, errors, _ = r10.validate(table[name]["category"], table[name]["message"])
        assert not ok
        assert errors
    oversized = table["fixture-output-oversized"]["message"]
    assert r10.validate("OUTPUT_READY", oversized)[0]
    assert len(r10.serialize(oversized)) > r10.MAX_ENVELOPE_BYTES


def test_utf8_byte_count_is_not_character_count():
    message = copy.deepcopy(cases()["fixture-clarify-valid"]["message"])
    message["payload"]["question"] = "¿Qué bomba?"
    assert len(r10.serialize(message)) > len(str(message["payload"]["question"]))


def test_empty_input_corpus_has_no_records(tmp_path):
    assert list(r10.input_cases(tmp_path)) == []


def test_captured_jsonl_is_ingested(tmp_path):
    message = cases()["fixture-output-valid"]["message"]
    (tmp_path / "captured.jsonl").write_text(__import__("json").dumps(message) + "\n", encoding="utf-8")
    found = list(r10.input_cases(tmp_path))
    assert len(found) == 1
    assert found[0]["category"] == "OUTPUT_READY"
    assert found[0]["provenance"].startswith("runtime_captured:")


def test_fixture_corpus_is_deterministic():
    assert r10.fixture_cases() == r10.fixture_cases()
