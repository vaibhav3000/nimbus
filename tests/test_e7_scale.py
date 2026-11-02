"""E7 tooling: the scale pack is valid, a simulated day completes the work, and replay at scale is identical."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import make_scale_pack  # noqa: E402
import run_e7  # noqa: E402

from cloud.pack import Pack  # noqa: E402


def test_scale_pack_has_three_clusters_and_the_requested_robots(tmp_path):
    make_scale_pack.build(3, 10, tmp_path / "scale10")
    p = Pack.load(str(tmp_path / "scale10"))
    clusters = p.fleet_doc["clusters"]
    assert len(clusters) == 3 and sum(len(c["robots"]) for c in clusters.values()) == 10
    assert [len(c["robots"]) for c in clusters.values()] == [4, 3, 3]


def test_a_simulated_morning_completes_the_work_and_more_robots_wait_less():
    arr = run_e7.arrivals(3, 1, [f"w{w}{s}" for w in range(3) for s in "ab"])
    small, big = run_e7.simulate(3, arr, 1), run_e7.simulate(10, arr, 1)
    assert small["completed"] == big["completed"] == len(arr)
    assert big["wait_s_p95"] <= small["wait_s_p95"] and big["missed_deadlines"] <= small["missed_deadlines"]
    assert small["decision_ms_p95"] > 0


def test_the_same_day_gives_the_same_decision_log():
    arr = run_e7.arrivals(2, 5, [f"w{w}{s}" for w in range(3) for s in "ab"])
    assert run_e7.simulate(6, arr, 5)["log_sha256"] == run_e7.simulate(6, arr, 5)["log_sha256"]


def test_arrivals_have_rounds_and_a_priority_mix_and_can_be_read_from_a_file(tmp_path):
    arr = run_e7.arrivals(6, 2, ["w0a"])
    assert any(sum(1 for a in arr if k * 7200 <= a["t"] < k * 7200 + 300) >= 6 for k in range(3))     # a burst at a round
    assert 0.1 < sum(a["priority"] == "urgent" for a in arr) / len(arr) < 0.45
    f = tmp_path / "a.csv"; f.write_text("t_seconds,priority,item,ward\n10,urgent,monitor,w0a\n")
    assert run_e7.read_arrivals(str(f)) == [{"t": 10.0, "priority": "urgent", "item": "monitor", "ward": "w0a"}]
