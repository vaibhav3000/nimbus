"""E5 (model outage) and E6 (duplicate / outdated / re-ordered messages, byte-identical re-run)."""

import subprocess
import sys
import threading
from pathlib import Path

import pytest

from cloud.clock import FixedClock
from cloud.faults import FaultyLLM
from cloud.llm import CircuitBreakerLLM, RuleBasedLLM
from cloud.pack import Pack
from cloud.trace import TracingLLM
from cloud_helpers import make_orch, new_task
from conftest import http_json

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import inject_messages  # noqa: E402
import replay_check  # noqa: E402


def outage_orch(pack="hospital"):
    clock = FixedClock()
    orch = make_orch(clock=clock, pack=Pack.load(pack))
    faulty = FaultyLLM(RuleBasedLLM(orch.pack, orch.topo), clock)
    orch.llm = TracingLLM(CircuitBreakerLLM(faulty, clock), orch.trace, orch.metrics)
    return orch, faulty, clock


def lease(orch, n, text="Deliver a wheelchair to 2B-3, routine."):
    t = new_task(n, text)
    out = orch.handle_request(t, text)
    t.update(spec=out.spec, lease=out.lease, status=out.kind, reservation=out.reservation, request_text=text)
    return t, out


def test_model_down_for_ten_minutes_leased_tasks_continue_new_requests_use_rules_and_the_model_comes_back():
    """FR-18 acceptance: calls forced to fail for 10 min mid-run; leased tasks continue; new requests still get a lease from the
    deterministic path; the requester is told it is reduced autonomy; and the model is used again afterwards."""
    orch, faulty, clock = outage_orch()
    a, out_a = lease(orch, 1)
    assert out_a.kind == "leased" and not out_a.notice and out_a.rows[0]["output"]["llm"]["calls"] == 1
    faulty.fail_for(600)                                              # the outage begins
    clock.advance(30)
    b, out_b = lease(orch, 2, "Deliver a blanket to 2A, routine.")
    assert out_b.kind == "leased" and "Reduced autonomy" in out_b.notice and out_b.rows[0]["output"]["proposed_by"] == "classical"
    assert faulty.failed_calls >= 1
    clock.advance(600)                                                # the ten minutes are over
    assert faulty.down is False
    clock.advance(31)                                                 # and the circuit breaker's cool-down has passed
    c, out_c = lease(orch, 3, "Deliver a monitor to 2A, routine.")
    assert out_c.kind == "leased" and not out_c.notice and out_c.rows[0]["output"]["llm"]["calls"] == 1


def test_leases_already_issued_keep_renewing_while_the_model_is_down():
    orch, faulty, clock = outage_orch()
    a, out = lease(orch, 1)
    faulty.fail_for(600)
    clock.advance(61 * 60)                                            # near the end of the lease
    renewed = orch.renew(a["task_id"], 1)
    assert renewed and renewed["version"] == 1 and faulty.failed_calls == 0       # renewal never touched the model


def test_the_outage_route_starts_and_ends_an_outage_over_http(api):
    assert http_json("POST", api + "/llm/outage", {"seconds": 600})[1]["model_down"] is True
    _, b = http_json("POST", api + "/requests", {"text": "Deliver a wheelchair to ward 3, routine", "message_id": "ou1"})
    assert "Reduced autonomy" in (b.get("notice") or "") and b["status"] == "leased"
    assert http_json("POST", api + "/llm/outage", {"seconds": 0})[1]["model_down"] is False
    _, b2 = http_json("POST", api + "/requests", {"text": "Deliver a wheelchair to ward 3, routine", "message_id": "ou2"})
    assert not b2.get("notice")
    assert http_json("POST", api + "/llm/outage", {"seconds": -5})[0] == 400 and http_json("POST", api + "/llm/outage", {"seconds": "x"})[0] == 400


def test_outage_can_be_scheduled_from_the_environment(monkeypatch):
    monkeypatch.setenv("NIMBUS_LLM_OUTAGE", "120:600")
    clock = FixedClock()
    f = FaultyLLM.from_env(RuleBasedLLM(Pack.load("hospital"), None), clock)
    assert not f.down
    clock.advance(121); assert f.down
    clock.advance(600); assert not f.down


# -- E6 -------------------------------------------------------------------------------------------------------------------------

def test_injector_repeated_outdated_and_reordered_messages_have_one_effect(api):
    r = inject_messages.repeat(api, 5)
    assert r == {"sent": 5, "accepted": 1, "acknowledged_as_duplicate": 4, "times_acted_on": 1}
    o = inject_messages.outdated(api)
    assert o["replanned_to"] == 2 and o["stale_escalation_status"] == 409 and o["stale_checkin_status"] == 409 and o["state_unchanged"]
    assert inject_messages.reorder(api)["order_independent"]


def test_injector_cli_reports_and_exits_zero(api, capsys):
    assert inject_messages.main(["--api", api]) == 0
    assert '"times_acted_on": 1' in capsys.readouterr().out


@pytest.mark.parametrize("seed", [3, 7])
def test_two_seeded_runs_in_separate_processes_give_identical_decision_logs(seed):
    one, two = replay_check.run_in_subprocess(seed, "1"), replay_check.run_in_subprocess(seed, "99")
    assert one and replay_check.compare(one, two) == [] and replay_check.digest(one) == replay_check.digest(two)


def test_the_comparison_notices_a_difference_and_ignores_timestamps():
    a = [{"who": "cloud", "output": {"decision": "x"}, "timestamp": "t1"}]
    assert replay_check.compare(a, [{"who": "cloud", "output": {"decision": "x"}, "timestamp": "t2"}]) == []
    assert replay_check.compare(a, [{"who": "cloud", "output": {"decision": "y"}, "timestamp": "t1"}])
    out = subprocess.run([sys.executable, str(SCRIPTS / "replay_check.py"), "--seed", "5"], capture_output=True, text=True)
    assert out.returncode == 0 and "IDENTICAL" in out.stdout
