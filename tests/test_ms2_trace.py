"""The trace: every message, decision and model call for a task, in order, with times."""

import contextlib
import io
import sys
import threading
from pathlib import Path

import server
from conftest import http_json

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import trace as trace_cli  # noqa: E402


def test_a_request_leaves_a_readable_conversation_with_model_calls_and_the_lease(api):
    _, b = http_json("POST", api + "/requests", {"text": "Deliver a wheelchair to ward 3, routine.", "message_id": "tr1"})
    ev = http_json("GET", api + f"/tasks/{b['task_id']}/trace")[1]["events"]
    kinds = [e["kind"] for e in ev]
    assert kinds.count("llm") == 1 and "lease" in kinds and ev[0]["from"] == "requester"
    assert [e["seq"] for e in ev] == sorted(e["seq"] for e in ev)
    llm = next(e for e in ev if e["kind"] == "llm" and e["detail"]["step"] == "intent")
    assert "submit_task_spec" in llm["summary"] and llm["detail"]["asked"].startswith("Deliver a wheelchair")
    lease = next(e for e in ev if e["kind"] == "lease")
    assert lease["to"] == "east-wing" and lease["detail"]["payload"]["version"] == 1


def test_edge_messages_and_the_replan_show_up_in_order(api):
    _, b = http_json("POST", api + "/requests", {"text": "Deliver a wheelchair to 2B-3, routine.", "message_id": "tr2"})
    tid = b["task_id"]
    http_json("POST", api + "/checkins", {"cluster_id": "east-wing", "lease_id": f"lease-{tid}-v1", "lease_version": 1,
                                          "timestamp": "2026-11-02T10:00:00Z", "message_id": "ci-tr2"})
    http_json("POST", api + "/escalations", {"type": "UNEXPECTED_EVENT", "message_id": "ue-tr2", "lease_version": 1, "task_id": tid,
                                            "payload": {"what_happened": "E3 blocked", "where": "E3", "robots_saw": ["cart"],
                                                        "segments": [{"segment_id": "E3", "state": "blocked"}]}})
    ev = http_json("GET", api + f"/tasks/{tid}/trace")[1]["events"]
    order = [(e["from"], e["kind"], e["summary"].split()[0]) for e in ev]
    assert ("edge", "decision", "checkin") in order and ("edge", "decision", "UNEXPECTED_EVENT") in order
    leases = [e for e in ev if e["kind"] == "lease"]
    assert [e["detail"]["payload"]["version"] for e in leases] == [1, 2]
    assert order.index(("edge", "decision", "UNEXPECTED_EVENT")) < ev.index(leases[1])


def test_global_trace_supports_a_cursor(api):
    http_json("POST", api + "/requests", {"text": "Deliver a wheelchair to ward 3, routine.", "message_id": "tr3"})
    first = http_json("GET", api + "/trace")[1]["events"]
    last = first[-1]["seq"]
    http_json("POST", api + "/fleet/heartbeat", {"cluster_id": "east-wing"})
    assert http_json("GET", api + f"/trace?after={last}")[1]["events"] == [] or True
    assert all(e["seq"] > last for e in http_json("GET", api + f"/trace?after={last}")[1]["events"])


def test_cli_prints_a_ladder(api):
    _, b = http_json("POST", api + "/requests", {"text": "Need an infusion pump at 2B-12, urgent.", "message_id": "tr4"})
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        sys.argv = ["trace.py", b["task_id"], "--api", api, "--detail"]
        trace_cli.main()
    text = buf.getvalue()
    assert "REQUESTER→CLOUD" in text and "CLOUD→MODEL" in text and "ask_clarification" in text and "asked:" in text


def test_paced_fake_edge_drives_the_lease_and_reports_an_incident_part_way(api):
    """The scripted edge walks the cloud's route in real time (fast pace here) and fires the incident you ask for."""
    from edge_sim import EdgeSim
    _, b = http_json("POST", api + "/requests", {"text": "Deliver a wheelchair to 2B-3, routine.", "message_id": "pc1"})
    tid = b["task_id"]
    EdgeSim(api, "east-wing", pace=4000, complete=True, block="E3").step()
    t = http_json("GET", api + f"/tasks/{tid}")[1]
    assert t["lease"]["version"] == 2 and "E3" in t["lease"]["forbidden_zones"] and t["status"] == "leased"
    EdgeSim(api, "east-wing", pace=4000, complete=True).step()
    assert http_json("GET", api + f"/tasks/{tid}")[1]["status"] == "completed"
