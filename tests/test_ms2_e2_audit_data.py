"""E2 sets, scorer and the synthetic-data audit."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import audit_data  # noqa: E402
import make_e2_sets  # noqa: E402
import score_e2  # noqa: E402

EVAL = Path(__file__).resolve().parents[1] / "evaluation" / "e2"


@pytest.fixture(autouse=True)
def _sim_map(monkeypatch):
    monkeypatch.setenv("NIMBUS_PACK", "hospital_sim")


def test_sets_have_the_sizes_the_prd_asks_for_and_say_they_are_drafts():
    r, rl, c = (json.loads((EVAL / f"{n}.json").read_text()) for n in ("requests", "rules", "context"))
    assert r["n"] == 50 and 12 <= r["vague"] <= 13 and rl["n"] == 10 and c["n"] == 20
    assert all("DRAFT" in d["status"] and d["synthetic"] is True for d in (r, rl, c))
    assert len({x["id"] for x in r["requests"]}) == 50
    assert json.dumps(make_e2_sets.build_requests(), sort_keys=True) == json.dumps(r, sort_keys=True)     # regenerates identically


def test_scoring_is_exact_on_a_hand_made_set():
    rows = [{"id": "a", "text": "t1", "gold": {"item": "wheelchair", "destination": "x", "priority": "routine"}},
            {"id": "b", "text": "t2", "gold": {"item": "monitor", "destination": "x", "priority": "urgent"}},
            {"id": "c", "text": "t3", "gold": {"ask": True, "about": "item"}},
            {"id": "d", "text": "t4", "gold": {"ask": True, "about": "item"}}]
    preds = {"t1": {"asked": False, "spec": {"item": "wheelchair", "destination_anchor": "x", "priority": "routine"}},
             "t2": {"asked": False, "spec": {"item": "monitor", "destination_anchor": "x", "priority": "routine"}},   # priority wrong
             "t3": {"asked": True, "spec": None}, "t4": {"asked": False, "spec": {"item": "x"}}}                     # d should have asked
    s = score_e2.score_requests(rows, lambda t: preds[t])
    assert s["field_accuracy"] == round(5 / 6, 3) and s["per_field"]["priority"] == 0.5
    assert s["clarification_recall"] == 0.5 and s["clarification_precision"] == 1.0
    assert {w["id"] for w in s["wrong"]} == {"b", "d"}


def test_a_question_about_a_clear_request_counts_against_the_details_and_against_precision():
    rows = [{"id": "a", "text": "t1", "gold": {"item": "wheelchair", "destination": "x"}}, {"id": "b", "text": "t2", "gold": {"ask": True}}]
    s = score_e2.score_requests(rows, lambda t: {"asked": True, "spec": None})
    assert s["field_accuracy"] == 0.0 and s["clarification_recall"] == 1.0 and s["clarification_precision"] == 0.5


@pytest.mark.parametrize("config", ["C1", "C3"])
def test_the_draft_sets_run_end_to_end_and_the_rules_and_habits_are_understood(config):
    rules = score_e2.run_rules(config, json.loads((EVAL / "rules.json").read_text()))
    ctx = score_e2.run_context(config, json.loads((EVAL / "context.json").read_text()))
    assert rules["rate"] == 1.0 and ctx["rate"] == 1.0 and ctx["by_kind"] == {"habit": "10/10", "control": "5/5", "override": "5/5"}


def test_the_dataset_audit_passes_on_the_repository_and_fails_on_personal_data(tmp_path, monkeypatch):
    assert audit_data.audit() == []
    (tmp_path / "evaluation" / "e2").mkdir(parents=True)
    (tmp_path / "evaluation" / "e2" / "a.json").write_text('{"text": "call Jane on 555-123-4567 or jane@example.com, MRN: 884422"}')
    (tmp_path / "evaluation" / "e2" / "b.json").write_text("{}")
    (tmp_path / "evaluation" / "manifest.json").write_text(json.dumps({"datasets": [
        {"path": "evaluation/e2/a.json", "synthetic": True}, {"path": "evaluation/e2/c.json", "synthetic": True}]}))
    found = " ".join(audit_data.audit(tmp_path))
    assert "e-mail address" in found and "phone number" in found and "medical record number" in found
    assert "evaluation/e2/b.json: not listed" in found and "c.json: listed but not found" in found
