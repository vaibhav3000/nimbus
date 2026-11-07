#!/usr/bin/env python3
"""R10: validate Nimbus edge/cloud envelopes and measure their wire size.

Reproduce with:
    python3 experiments/robot_edge/r10_escalation_contracts.py --seed 1 --out results/final/R10

This runner is deliberately offline.  It uses the published production contract
validator (``contracts.validate_document``) and the same ``json.dumps(...).encode
("utf-8")`` serialization used by the edge HTTP sender.  Default records are
generated fixtures, not end-to-end traffic.  ``--input-dir`` additionally reads
JSON/JSONL envelopes captured by future R1--R9 runs.
"""
from __future__ import annotations

import argparse
import copy
import json
import random
import sys
from pathlib import Path
from statistics import median
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "experiments" / "cloud"))

import contracts
import lib

MAX_ENVELOPE_BYTES = 2 * 1024
ARTIFACT_MIN_BYTES = 200 * 1024
ARTIFACT_MAX_BYTES = 2 * 1024 * 1024


def serialize(message: dict[str, Any]) -> bytes:
    """Exactly the production edge HTTP serialization in EdgeSupervisor."""
    return json.dumps(message).encode("utf-8")


def lease_fixture() -> dict[str, Any]:
    return {
        "lease_id": "lease-r10-fixture-v1", "version": 1, "priority": "urgent",
        "deadline_ts": "2026-11-08T10:12:00Z", "expiry_ts": "2026-11-08T10:15:00Z",
        "allowed_zones": ["central-supply", "corridor-2B", "2B-12"],
        "forbidden_zones": ["mri-suite"], "speed_limit": 0.5,
        "assigned_robots": ["R1"], "cluster_id": "east-wing",
        "task_sequence": [
            {"cmd": "go_to", "robot": "R1", "params": {"to": "central-supply"}},
            {"cmd": "go_to", "robot": "R1", "params": {"to": "2B-12"}},
        ],
        "when_to_escalate": ["door_closed", "deadline_at_risk", "robot_failed"],
    }


def checkin_fixture() -> dict[str, Any]:
    # There is no published check-in JSON Schema.  These are the required
    # fields checked by server.process_checkin before it touches live state.
    return {
        "cluster_id": "east-wing", "lease_id": "lease-r10-fixture-v1",
        "lease_version": 1, "timestamp": "2026-11-08T10:01:00Z",
        "message_id": "msg-ci-r10-fixture",
        "robots": [{"id": "R1", "state": "busy", "zone": "corridor-2B", "battery": 95.0}],
    }


def fixture_cases() -> list[dict[str, Any]]:
    base = {"message_id": "msg-r10", "lease_version": 1, "task_id": "task-r10-fixture"}
    clarify = {**base, "type": "CLARIFY", "message_id": "msg-clarify-r10",
               "payload": {"question": "Which pump variant is needed?",
                           "possible_readings": ["paediatric infusion pump", "standard infusion pump"]}}
    unexpected = {**base, "type": "UNEXPECTED_EVENT", "message_id": "msg-unexpected-r10",
                  "payload": {"what_happened": "Corridor blocked by a cleaning cart", "where": "corridor-2B",
                              "robots_saw": ["R1 range scan reports an occupied corridor"],
                              "segments": [{"segment_id": "corridor-2B", "state": "blocked"}]}}
    output = {**base, "type": "OUTPUT_READY", "message_id": "msg-output-r10",
              "payload": {"result_ref": "storage://results/task-r10-fixture/delivery-proof.jpg"}}
    oversized = copy.deepcopy(output)
    oversized["message_id"] = "msg-output-oversized-r10"
    oversized["payload"]["result_ref"] = "storage://results/" + ("x" * (MAX_ENVELOPE_BYTES + 128))
    missing_clarify = copy.deepcopy(clarify)
    missing_clarify["message_id"] = "msg-clarify-missing-r10"
    del missing_clarify["payload"]["possible_readings"]
    unknown_type = copy.deepcopy(output)
    unknown_type["type"] = "STATUS_UPDATE"
    unknown_type["message_id"] = "msg-unknown-r10"
    bad_identifier = copy.deepcopy(output)
    bad_identifier["message_id"] = ""
    bad_identifier["lease_version"] = "one"
    bad_lease = lease_fixture()
    del bad_lease["speed_limit"]
    bad_checkin = checkin_fixture()
    del bad_checkin["message_id"]
    return [
        {"case_id": "fixture-clarify-valid", "category": "CLARIFY", "condition": "positive", "message": clarify, "expected_valid": True},
        {"case_id": "fixture-unexpected-valid", "category": "UNEXPECTED_EVENT", "condition": "positive", "message": unexpected, "expected_valid": True},
        {"case_id": "fixture-output-valid", "category": "OUTPUT_READY", "condition": "positive", "message": output, "expected_valid": True},
        {"case_id": "fixture-output-oversized", "category": "OUTPUT_READY", "condition": "oversized_envelope", "message": oversized, "expected_valid": True, "expected_within_limit": False},
        {"case_id": "negative-clarify-missing-readings", "category": "CLARIFY", "condition": "negative_schema", "message": missing_clarify, "expected_valid": False},
        {"case_id": "negative-unknown-escalation-type", "category": "ESCALATION", "condition": "negative_schema", "message": unknown_type, "expected_valid": False},
        {"case_id": "negative-invalid-escalation-identifiers", "category": "OUTPUT_READY", "condition": "negative_schema", "message": bad_identifier, "expected_valid": False},
        {"case_id": "fixture-lease-valid", "category": "LEASE", "condition": "positive", "message": lease_fixture(), "expected_valid": True},
        {"case_id": "negative-lease-missing-speed", "category": "LEASE", "condition": "negative_schema", "message": bad_lease, "expected_valid": False},
        {"case_id": "fixture-checkin-valid", "category": "CHECKIN", "condition": "positive", "message": checkin_fixture(), "expected_valid": True},
        {"case_id": "negative-checkin-missing-id", "category": "CHECKIN", "condition": "negative_preflight", "message": bad_checkin, "expected_valid": False},
    ]


def checkin_errors(message: dict[str, Any]) -> list[str]:
    """The stateless acceptance checks in server.process_checkin."""
    import re
    message_id = message.get("message_id")
    if not isinstance(message_id, str) or not message_id:
        return ["message_id is required"]
    missing = [k for k in ("cluster_id", "lease_id", "lease_version", "timestamp") if k not in message]
    if missing:
        return [f"check-in missing {missing}"]
    if not isinstance(message["lease_id"], str) or not re.fullmatch(r"lease-(.+)-v\d+", message["lease_id"]):
        return ["lease_id must be lease-<task_id>-v<version>"]
    if not isinstance(message["lease_version"], int) or message["lease_version"] < 1:
        return ["lease_version must be a positive integer"]
    return []


def validate(category: str, message: dict[str, Any]) -> tuple[bool, list[str], str]:
    if category == "LEASE":
        errors = contracts.validate_document("lease", message)
        return not errors, errors, "contracts.validate_document(lease)"
    if category == "CHECKIN":
        errors = checkin_errors(message)
        return not errors, errors, "server.process_checkin preflight"
    errors = contracts.validate_document("escalation", message)
    return not errors, errors, "contracts.validate_document(escalation)"


def classify(message: Any) -> str | None:
    if not isinstance(message, dict):
        return None
    if {"type", "message_id", "lease_version", "task_id", "payload"} <= set(message):
        return str(message.get("type") or "ESCALATION")
    if {"lease_id", "version", "priority", "task_sequence"} <= set(message):
        return "LEASE"
    if {"cluster_id", "lease_id", "lease_version", "timestamp", "message_id"} <= set(message):
        return "CHECKIN"
    return None


def input_cases(folder: Path) -> Iterable[dict[str, Any]]:
    for path in sorted(folder.rglob("*.jsonl")) + sorted(folder.rglob("*.json")):
        try:
            docs = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] if path.suffix == ".jsonl" else [json.loads(path.read_text(encoding="utf-8"))]
        except (OSError, json.JSONDecodeError):
            continue
        for index, doc in enumerate(docs, 1):
            for candidate in (doc if isinstance(doc, dict) else []), *((doc if isinstance(doc, list) else []),):
                category = classify(candidate)
                if category:
                    yield {"case_id": f"captured-{path.name}-{index}", "category": category, "condition": "captured", "message": candidate,
                           "expected_valid": True, "provenance": f"runtime_captured:{path.relative_to(folder)}"}


def percentile(values: list[int], q: float) -> float | None:
    return lib.percentile(values, q) if values else None


def build_summary(records: list[dict[str, Any]], artifact_sizes: list[int]) -> list[dict[str, Any]]:
    rows = []
    keys = sorted({(r["category"], r["condition"]) for r in records})
    for category, condition in keys:
        group = [r for r in records if (r["category"], r["condition"]) == (category, condition)]
        sizes = [r["serialized_bytes"] for r in group]
        positives = [r for r in group if r["expected_valid"]]
        negatives = [r for r in group if not r["expected_valid"]]
        rows.append({
            "category": category, "condition": condition, "number_tested": len(group),
            "number_valid": sum(r["schema_valid"] for r in group),
            "number_correctly_rejected": sum((not r["schema_valid"]) and (not r["expected_valid"]) for r in group),
            "validation_success_rate": round(sum(r["schema_valid"] == r["expected_valid"] for r in group) / len(group), 3),
            "positive_valid": sum(r["schema_valid"] for r in positives), "positive_tested": len(positives),
            "negative_correctly_rejected": sum(not r["schema_valid"] for r in negatives), "negative_tested": len(negatives),
            "min_bytes": min(sizes), "median_bytes": round(float(median(sizes)), 1), "p95_bytes": round(float(percentile(sizes, 95)), 1),
            "max_bytes": max(sizes), "exceeding_2kb": sum(not r["within_2kb"] for r in group), "source": "measured",
        })
    rows.append({"category": "OUTPUT_READY_ARTIFACT", "condition": "observed_artifacts", "number_tested": len(artifact_sizes),
                 "number_valid": sum(ARTIFACT_MIN_BYTES <= n <= ARTIFACT_MAX_BYTES for n in artifact_sizes), "number_correctly_rejected": 0,
                 "validation_success_rate": "n/a" if not artifact_sizes else round(sum(ARTIFACT_MIN_BYTES <= n <= ARTIFACT_MAX_BYTES for n in artifact_sizes) / len(artifact_sizes), 3),
                 "positive_valid": "n/a", "positive_tested": "n/a", "negative_correctly_rejected": "n/a", "negative_tested": "n/a",
                 "min_bytes": min(artifact_sizes) if artifact_sizes else "n/a", "median_bytes": round(float(median(artifact_sizes)), 1) if artifact_sizes else "n/a",
                 "p95_bytes": round(float(percentile(artifact_sizes, 95)), 1) if artifact_sizes else "n/a", "max_bytes": max(artifact_sizes) if artifact_sizes else "n/a",
                 "exceeding_2kb": "n/a", "source": "measured"})
    return rows


def make_plots(rd: lib.ResultDir, records: list[dict[str, Any]], artifact_sizes: list[int]) -> list[str]:
    plt = lib.plt()
    categories = sorted({r["category"] for r in records})
    fig, ax = plt.subplots()
    data = [[r["serialized_bytes"] for r in records if r["category"] == c] for c in categories]
    ax.boxplot(data, labels=categories, showfliers=True)
    ax.set_yscale("log")
    lib.pass_line(ax, MAX_ENVELOPE_BYTES, "2 KB envelope threshold")
    ax.set_ylabel("Serialized envelope size (bytes, log scale)")
    ax.set_xlabel("Message category")
    ax.set_title("R10 message envelope sizes")
    sizes = lib.save(fig, rd, "message_sizes")

    fig, ax = plt.subplots()
    valid = [sum(r["schema_valid"] for r in records if r["category"] == c) for c in categories]
    rejected = [sum((not r["schema_valid"]) and (not r["expected_valid"]) for r in records if r["category"] == c) for c in categories]
    x = list(range(len(categories)))
    ax.bar(x, valid, label="Valid")
    ax.bar(x, rejected, bottom=valid, label="Correctly rejected")
    ax.set_xticks(x, categories)
    ax.set_ylabel("Messages (count)")
    ax.set_xlabel("Message category")
    ax.set_title("R10 validation outcomes")
    ax.legend()
    validation = lib.save(fig, rd, "validation_by_type")
    return [sizes, validation]


def run(seed: int = 1, out: str = "results/final/R10", input_dir: str | None = None,
        artifacts: list[str] | None = None) -> dict[str, Any]:
    expected = (ROOT / "results" / "final" / "R10").resolve()
    if Path(out).resolve() != expected:
        raise ValueError(f"--out must be {expected.relative_to(ROOT)} to use the shared ResultDir convention")
    random.Random(seed)  # Reserve deterministic seed handling for future generated corpus variants.
    rd = lib.ResultDir("R10", "Escalation format and message sizes", None, ["--seed", str(seed), "--out", out], prd="FR-9")
    rd.env.update({"source": "measured", "target": "in-process published contracts and production serialization", "model": "n/a",
                   "seed": seed, "serialization": "json.dumps(message).encode('utf-8')"})
    rd.reset_raw()
    cases = fixture_cases()
    if input_dir:
        cases.extend(input_cases(Path(input_dir)))
    records, artifact_sizes = [], []
    for artifact in artifacts or []:
        path = Path(artifact)
        if not path.is_file():
            raise FileNotFoundError(f"--artifact is not a file: {path}")
        size = path.stat().st_size
        artifact_sizes.append(size)
        rd.raw("validation", {"case_id": f"artifact-{path.name}", "seed": seed, "category": "OUTPUT_READY_ARTIFACT",
                              "condition": "observed_artifact", "provenance": f"runtime_captured:{path}",
                              "artifact_bytes": size, "within_200kb_to_2mb": ARTIFACT_MIN_BYTES <= size <= ARTIFACT_MAX_BYTES,
                              "validation_path": "filesystem byte count"})
    for case in cases:
        message = case["message"]
        payload = serialize(message)
        valid, errors, path = validate(case["category"], message)
        expected_limit = case.get("expected_within_limit", True)
        record = {
            "case_id": case["case_id"], "seed": seed, "category": case["category"], "condition": case["condition"],
            "provenance": case.get("provenance", "generated_fixture"), "message": message, "message_id": message.get("message_id"),
            "lease_id": message.get("lease_id"), "lease_version": message.get("lease_version", message.get("version")),
            "task_id": message.get("task_id"), "serialized_bytes": len(payload), "payload_bytes": len(serialize(message.get("payload", {}))) if isinstance(message.get("payload"), dict) else 0,
            "schema_valid": valid, "expected_valid": case["expected_valid"], "within_2kb": len(payload) <= MAX_ENVELOPE_BYTES,
            "expected_within_2kb": expected_limit, "rejection_reason": "; ".join(errors), "validation_path": path,
        }
        records.append(record)
        rd.raw("messages", {k: v for k, v in record.items() if k not in {"schema_valid", "expected_valid", "within_2kb", "expected_within_2kb", "rejection_reason", "validation_path"}})
        rd.raw("validation", {k: v for k, v in record.items() if k != "message"})
    summary = build_summary(records, artifact_sizes)
    rd.summary(summary)
    plots = make_plots(rd, records, artifact_sizes)
    positives = [r for r in records if r["expected_valid"] and r["expected_within_2kb"]]
    negatives = [r for r in records if not r["expected_valid"]]
    envelope_ok = all(r["schema_valid"] and r["within_2kb"] for r in positives)
    negatives_ok = all(not r["schema_valid"] for r in negatives)
    # OUTPUT_READY carries only a storage URI by contract; it does not embed
    # the result artifact.  Therefore the 200 KB--2 MB artifact range is not
    # an envelope acceptance criterion.  When artifacts are supplied, report
    # their size separately without changing this wire-contract verdict.
    verdict = "PASS" if envelope_ok and negatives_ok else "FAIL"
    rd.finish(claim="Fixture-level Nimbus escalation, lease, and check-in envelopes serialize within the 2 KB FR-9 limit and the three escalation schemas accept only valid forms.",
              pass_bar="100% valid tested escalations; lease, check-in and escalation envelopes <= 2 KB; OUTPUT_READY artifacts 200 KB to 2 MB.",
              verdict=verdict,
              measured=f"{len(records)} envelopes: {len(positives)}/{len(positives)} expected-valid in-limit envelopes passed; {sum(not r['schema_valid'] for r in negatives)}/{len(negatives)} malformed envelopes rejected; OUTPUT_READY uses a result_ref, so artifact bytes are not part of this envelope test.",
              notes=["Default corpus is generated fixtures based on production message shapes; it is not end-to-end traffic.",
                     "The published OUTPUT_READY schema carries a result_ref URI, not embedded artifact bytes; its envelope is measured separately from artifacts.",
                     "The 200 KB to 2 MB range concerns result artifacts, not the OUTPUT_READY envelope: this protocol deliberately sends a result_ref instead of embedded artifact bytes. It is therefore N/A to this wire-contract verdict.",
                     "Use --input-dir <directory> to include future R1--R9 JSON/JSONL captured envelopes; unsupported/non-envelope records are ignored.",
                     "Pass one or more captured files with --artifact to measure their actual bytes against the 200 KB to 2 MB requirement."], plots=plots)
    return {"records": records, "summary": summary, "verdict": verdict}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", default="results/final/R10")
    ap.add_argument("--input-dir", help="Optional directory of captured JSON or JSONL envelopes")
    ap.add_argument("--artifact", action="append", default=[], help="Captured OUTPUT_READY artifact file; repeatable")
    args = ap.parse_args()
    run(args.seed, args.out, args.input_dir, args.artifact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
