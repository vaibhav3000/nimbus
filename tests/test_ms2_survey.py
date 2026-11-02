"""T7: inventory sweep. A new task type as a pack template; several robots; edge-filtered report merged into the cloud's picture."""

import json

import contracts
from cloud.pack import Pack
from cloud.results import ResultStore
from cloud_helpers import LLMError, make_orch, new_task, tool
from conftest import http_json

SIM = Pack.load("hospital_sim")
ASK = "Count the idle infusion pumps"


def test_survey_request_gets_one_lease_with_a_visit_and_photo_per_place_and_one_report():
    orch = make_orch(pack=SIM)
    out = orch.handle_request(new_task(), ASK)
    assert out.kind == "leased" and out.spec["task"] == "survey"
    cmds = [(s["cmd"], s["params"].get("to")) for s in out.lease["task_sequence"]]
    visited = [t for c, t in cmds if c == "go_to"]
    assert sorted(out.spec["targets"]) == sorted(visited) and cmds.count(("photo", None)) == len(visited) and cmds[-1][0] == "report"
    assert contracts.validate_document("lease", out.lease) == [] and set(out.lease) >= {"lease_id", "version", "task_sequence"}
    assert out.rows[0]["output"]["survey"]["assign"]


def test_places_not_in_this_cluster_become_a_second_task_for_the_other_cluster():
    orch = make_orch(pack=SIM)
    out = orch.handle_request(new_task(), ASK)
    assert len(out.children) == 1 and out.children[0]["cluster_hint"] != out.lease["cluster_id"]


def test_two_robots_split_the_places_between_them():
    orch = make_orch()                                      # hand-made map: three east robots
    out = orch.handle_request(new_task(), "Inventory sweep of infusion pumps in 2A and 2B and ward 3")
    assert out.kind == "leased" and len(out.lease["assigned_robots"]) == 2
    per_robot = {r: [s["params"]["to"] for s in out.lease["task_sequence"] if s.get("robot") == r and s["cmd"] == "go_to"] for r in out.lease["assigned_robots"]}
    assert all(per_robot.values()) and sorted(sum(per_robot.values(), [])) == ["2A", "2B", "ward-3"]
    claims = [f"robot:{r}" for r in out.lease["assigned_robots"]]
    assert all(orch.repo.holder(c, orch.clock.now()) for c in claims)


def test_a_restricted_place_is_skipped_and_reported_not_a_reason_to_cancel_the_sweep():
    orch = make_orch()
    orch.policies.declare("2B", "isolation")
    out = orch.handle_request(new_task(), "Inventory sweep of infusion pumps in 2A and 2B")
    assert out.kind == "leased" and "2B" not in [s["params"].get("to") for s in out.lease["task_sequence"]]
    assert out.rows[0]["output"]["survey"]["skipped"][0]["target"] == "2B"


def test_one_free_robot_means_a_shorter_sweep_and_none_free_queues():
    orch = make_orch()
    for r in ("R2", "R3"):
        orch.fleet.clusters["east-wing"]["robots"][r]["state"] = "failed"
    out = orch.handle_request(new_task(1), "Inventory sweep of infusion pumps in 2A and 2B")
    assert out.kind == "leased" and out.lease["assigned_robots"] == ["R1"]
    again = orch.handle_request(new_task(2), "Inventory sweep of infusion pumps in 2A and 2B")
    assert again.kind == "queued" and "no free robot" in again.reason


def test_the_model_can_ask_for_a_survey_and_a_bad_place_is_corrected_then_falls_back_to_rules():
    orch = make_orch(override=lambda tools, kw: tool("submit_survey", item="infusion_pump", places=["2A"], priority="routine", deadline_minutes=60, rationale="r") if "submit_survey" in tools else None)
    out = orch.handle_request(new_task(), "please tell me where pumps are")
    assert out.kind == "leased" and out.spec["targets"] == ["2A"]
    bad = make_orch(override=lambda tools, kw: tool("submit_survey", item="infusion_pump", places=["Narnia"], priority="routine", deadline_minutes=60, rationale="r") if "submit_survey" in tools else None)
    out = bad.handle_request(new_task(), ASK)
    assert out.kind == "leased" and "Reduced autonomy" in (out.notice or "")


def test_survey_works_in_c3_with_no_model():
    orch = make_orch(mode="C3")
    assert orch.handle_request(new_task(), ASK).kind == "leased" and orch.llm.calls == []


def test_the_edge_report_is_merged_into_place_inventory_and_summarised(tmp_path):
    orch = make_orch(); orch.results = ResultStore(root=tmp_path)
    (tmp_path / "r").mkdir()
    (tmp_path / "r" / "survey.json").write_text(json.dumps({"reports": [
        {"robot": "R1", "counts": {"2A": {"infusion_pump": 2}}}, {"robot": "R2", "counts": {"ward-3": {"infusion_pump": 1}, "nowhere": {"infusion_pump": 9}}}]}))
    t = new_task(1, "x"); out = orch.handle_request(t, "Inventory sweep of infusion pumps in 2A and ward 3")
    t.update(spec=out.spec, lease=out.lease)
    done = orch.complete_summary(t, "storage://r/survey.json")
    assert done["merged"] == 2 and "3 x infusion pump" in done["text"] and done["calls"] == 0
    assert orch.fleet.place_stock("2A") == {"infusion_pump": 2} and orch.fleet.place_stock("ward-3") == {"infusion_pump": 1}
    assert orch.fleet.place_stock("nowhere") == {}                       # unknown places are ignored
    assert "no readable report" in orch.surveyor.merge(t, "not json")[0]


def test_survey_end_to_end_over_http(api):
    _, b = http_json("POST", api + "/requests", {"text": "Inventory sweep of infusion pumps in 2A and 2B", "message_id": "sv1"})
    t = http_json("GET", api + f"/tasks/{b['task_id']}")[1]
    assert t["status"] == "leased" and t["spec"]["task"] == "survey"
    done = {"type": "OUTPUT_READY", "message_id": "sv-o", "lease_version": 1, "task_id": b["task_id"], "payload": {"result_ref": "storage://none"}}
    assert http_json("POST", api + "/escalations", done)[0] == 201
    assert http_json("GET", api + f"/tasks/{b['task_id']}")[1]["status"] == "completed"
