"""E3 harness: every task runs under every configuration, results are repeatable, and the report renders."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import e3_report  # noqa: E402
import run_e3  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _sim_map(monkeypatch):
    monkeypatch.setenv("NIMBUS_PACK", "hospital_sim")      # the runner's tasks are written for P1's map; conftest pins the older one


COLUMNS = {"seed", "task", "config", "success", "deadline_met", "llm_calls", "tokens_in", "tokens_out", "cloud_decisions", "wan_bytes",
           "cost_usd", "modelled_latency_s", "modelled_completion_s", "note"}


def rows(seeds=1, only=None):
    return run_e3.run(seeds, run_e3.PRICE_IN, run_e3.PRICE_OUT, only)


def test_every_task_runs_under_every_configuration_with_the_declared_columns():
    r = rows()
    assert {(x["task"], x["config"]) for x in r} == {(t, c) for t in run_e3.TASKS for c in run_e3.CONFIGS}
    assert all(set(x) == COLUMNS for x in r)


def test_the_three_configurations_behave_as_the_prd_describes():
    by = {(x["task"], x["config"]): x for x in rows()}
    for t in run_e3.TASKS:
        assert by[(t, "C3")]["llm_calls"] == 0                                   # no model anywhere
    assert by[("T1", "C1")]["llm_calls"] < by[("T1", "C2")]["llm_calls"]         # C2 pays a round trip per step
    assert by[("T7", "C1")]["wan_bytes"] < by[("T7", "C2")]["wan_bytes"] / 10    # only C1's edge filters before sending
    assert all(by[(t, c)]["success"] for t in ("T1", "T5", "T6", "T7", "T8") for c in run_e3.CONFIGS)
    assert all(by[("T4", c)]["success"] for c in run_e3.CONFIGS)                  # the detour is inside the lease in every config


def test_two_runs_are_identical_and_nfr4_is_met():
    a, b = rows(2), rows(2)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    s = run_e3.summarise(a, 2)
    assert s["NFR-4"]["met"] and s["NFR-4"]["ratio_c1_over_c2"] <= 0.5


def test_report_renders_a_table_row_per_cell_and_labels_the_stand_in(tmp_path):
    r = rows(1, ["T1", "T7"])
    doc = {"seeds": 1, "pack": "hospital_sim", "model": "stand-in", "summary": run_e3.summarise(r, 1), "rows": r}
    text = e3_report.render(doc)
    assert text.count("| T1") == 3 and text.count("| T7") == 3 and "stand-in" in text and "NFR-4" in text
