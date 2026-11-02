"""MS2 Task 6: plain-language rules about a place, equipment moved out, several items per message (SC-3)."""

from cloud_helpers import LLMError, make_orch, new_task, tool
from conftest import http_json

SC3 = "Ward 2C is now isolation. Get the spare pumps out, hold routine deliveries there, urgent ones can still go in."


def test_sc3_sentence_declares_the_restriction_and_creates_one_extraction_task_per_pump():
    orch = make_orch()
    out = orch.handle_request(new_task(), SC3)
    assert out.kind == "statement" and "2C" in orch.policies.restrictions()
    assert len(out.children) == 2 and all(c["source"] == "2C" and c["destination"] == "W-STORE" for c in out.children)
    assert {c["item"] for c in out.children} == {"infusion_pump"} and all(c["priority"] == "urgent" for c in out.children)
    assert {r["output"]["decision"] for r in out.rows} >= {"declare_restriction", "notify"}


def test_audiences_are_told_ward_operator_and_the_affected_clusters():
    orch = make_orch()
    orch.handle_request(new_task(), SC3)
    assert any(n["zone"] == "2C" for n in orch.notifier.for_audience("place"))
    assert orch.notifier.for_audience("operator")
    assert any(n["cluster_id"] == "west-wing" and n["type"] == "MAP" for n in orch.notifier.for_audience("cluster"))


def test_repeating_the_statement_changes_nothing():
    orch = make_orch()
    orch.handle_request(new_task(), SC3)
    again = orch.handle_request(new_task(2), SC3)
    assert again.kind == "statement" and again.children == [] and "already" in again.reason


def test_unknown_place_asks_instead_of_half_applying():
    orch = make_orch()
    out = orch.handle_request(new_task(), "Ward 9Z is now isolation, get the pumps out.")
    assert out.kind == "clarify" and orch.policies.restrictions() == {}


def test_lifting_releases_held_work():
    orch = make_orch()
    orch.handle_request(new_task(), "Ward 2C is now isolation.")
    held = new_task(5, "x")
    out = orch.handle_request(held, "Deliver a blanket to 2C, routine")
    assert out.kind == "queued"
    held.update(status="queued", spec=out.spec, request_text="Deliver a blanket to 2C, routine")
    lift = orch.handle_request(new_task(6), "Ward 2C is no longer in isolation.")
    assert lift.kind == "statement" and lift.drain and orch.policies.restrictions() == {}
    done = orch.drain(lambda tid: held if tid == held["task_id"] else None)
    assert len(done) == 1 and done[0][1].kind == "leased"


def test_in_flight_routine_lease_is_stopped_and_held_while_urgent_is_untouched():
    orch = make_orch()
    routine, urgent = new_task(1, "r"), new_task(2, "u")
    r_out = orch.handle_request(routine, "Deliver a blanket to 2C, routine")
    u_out = orch.handle_request(urgent, "Deliver a blanket to 2C, urgent")
    for t, o, txt in ((routine, r_out, "Deliver a blanket to 2C, routine"), (urgent, u_out, "Deliver a blanket to 2C, urgent")):
        t.update(status="leased", spec=o.spec, lease=o.lease, reservation=o.reservation, request_text=txt)
    tasks = {routine["task_id"]: routine, urgent["task_id"]: urgent}
    orch.load_task = tasks.get
    out = orch.handle_request(new_task(3), SC3)
    held = [t for t, o in out.others]
    assert held == [routine] and out.others[0][1].kind == "held"
    assert out.others[0][1].publish[0][1]["task_sequence"][0]["cmd"] == "stop"
    assert orch.repo.holder(f"robot:{r_out.lease['assigned_robots'][0]}", orch.clock.now()) is None
    assert orch.repo.holder(f"robot:{u_out.lease['assigned_robots'][0]}", orch.clock.now()) is not None
    assert [e.task_id for e in orch.scheduler.ordered()] == [routine["task_id"]]


def test_a_held_task_gets_the_next_lease_version_when_released():
    orch = make_orch()
    routine = new_task(1, "r")
    text = "Deliver a blanket to 2C, routine"
    o1 = orch.handle_request(routine, text)
    routine.update(status="leased", spec=o1.spec, lease=o1.lease, reservation=o1.reservation, request_text=text)
    orch.load_task = lambda tid: routine if tid == routine["task_id"] else None
    out = orch.handle_request(new_task(3), "Ward 2C is now isolation.")
    routine.update(status="queued", lease=out.others[0][1].lease)
    orch.handle_request(new_task(4), "Ward 2C is no longer in isolation.")
    again = orch.drain(lambda tid: routine)
    assert again[0][1].lease["version"] == 3          # v1 original, v2 the stop lease, v3 the resumed task


def test_sc3_works_with_the_model_down_through_the_rules():
    def down(tools, kw):
        raise LLMError("down")
    orch = make_orch(override=down)
    out = orch.handle_request(new_task(), SC3)
    assert out.kind == "statement" and len(out.children) == 2 and "Reduced autonomy" in (out.notice or "")


def test_c3_reads_the_statement_with_no_model():
    orch = make_orch(mode="C3")
    out = orch.handle_request(new_task(), SC3)
    assert out.kind == "statement" and orch.llm.calls == []


def test_the_model_can_announce_a_restriction_itself():
    orch = make_orch(override=lambda tools, kw: tool("announce_restriction", zone="2C", kind="isolation",
                                                     extract_items=["infusion_pump"], rationale="r") if "announce_restriction" in tools else None)
    out = orch.handle_request(new_task(), "please treat 2C as an infectious ward and clear the pumps")
    assert out.kind == "statement" and len(out.children) == 2


def test_model_naming_an_unknown_place_is_corrected_then_falls_back_to_rules():
    orch = make_orch(override=lambda tools, kw: tool("announce_restriction", zone="Narnia", kind="isolation", rationale="r")
                     if "announce_restriction" in tools else None)
    out = orch.handle_request(new_task(), SC3)
    assert out.kind == "statement" and "2C" in orch.policies.restrictions() and "Reduced autonomy" in (out.notice or "")


def test_routine_to_the_isolated_ward_is_held_and_urgent_goes_in():
    orch = make_orch()
    orch.handle_request(new_task(), SC3)
    assert orch.handle_request(new_task(2), "Deliver a blanket to 2C, routine").kind == "queued"
    assert orch.handle_request(new_task(3), "Deliver a blanket to 2C, urgent").kind == "leased"


def test_extraction_moves_the_inventory_into_the_store_when_the_robot_reports_done():
    orch = make_orch()
    out = orch.handle_request(new_task(), SC3)
    child = new_task(7, "x")
    c_out = orch.allocate_spec(child, out.children[0])
    assert c_out.kind == "leased" and c_out.lease["cluster_id"] == "west-wing"
    child.update(spec=c_out.spec or out.children[0], lease=c_out.lease)
    before = orch.fleet.clusters["west-wing"]["stock"]["infusion_pump"]
    orch.finish(child)
    assert orch.fleet.place_stock("2C") == {"infusion_pump": 1}
    assert orch.fleet.clusters["west-wing"]["stock"]["infusion_pump"] == before + 1


# -- several items in one message -----------------------------------------------------------------------------------------

def test_two_items_in_one_message_make_two_tasks():
    orch = make_orch()
    out = orch.handle_request(new_task(), "Deliver a wheelchair and an iv pole to ward 3, routine")
    assert out.kind == "leased" and len(out.children) == 1
    assert {out.spec["item"], out.children[0]["item"]} == {"wheelchair", "iv_pole"}


# -- over HTTP -----------------------------------------------------------------------------------------------------------

def test_sc3_through_http_creates_child_tasks_and_holds_routine_work(api):
    st, b = http_json("POST", api + "/requests", {"text": SC3, "message_id": "m-sc3"})
    assert st == 201 and b["status"] == "completed"
    _, parent = http_json("GET", api + f"/tasks/{b['task_id']}")
    assert len(parent["children"]) == 2
    for cid in parent["children"]:
        _, child = http_json("GET", api + f"/tasks/{cid}")
        assert child["status"] == "leased" and child["lease"]["cluster_id"] == "west-wing" and child["parent"] == b["task_id"]
    st, held = http_json("POST", api + "/requests", {"text": "Deliver a blanket to 2C, routine", "message_id": "m-held"})
    assert http_json("GET", api + f"/tasks/{held['task_id']}")[1]["status"] == "queued"
    _, notes = http_json("GET", api + "/notifications?audience=operator")
    assert notes["notifications"]


def test_two_items_over_http_create_two_leased_tasks(api):
    st, b = http_json("POST", api + "/requests", {"text": "Deliver a wheelchair and an iv pole to ward 3, routine", "message_id": "m-two"})
    _, parent = http_json("GET", api + f"/tasks/{b['task_id']}")
    assert len(parent["children"]) == 1
    assert http_json("GET", api + f"/tasks/{parent['children'][0]}")[1]["status"] == "leased"
