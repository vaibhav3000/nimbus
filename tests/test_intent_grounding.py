"""The model's task is checked against the requester's words (found by experiment C1 on Bedrock Nova Lite):
a destination nobody named is asked for, and priority follows the wording, not a stated deadline alone."""

from cloud.pack import Pack
from cloud_helpers import make_orch, new_task, tool

SIM6 = Pack.load("hospital_sim6")


def model_says(**spec):
    def override(tools, kw):
        if "submit_task_spec" in tools:
            return tool("submit_task_spec", rationale="model", **spec)
    return override


def test_a_destination_the_requester_never_named_is_asked_for():
    orch = make_orch(pack=SIM6, override=model_says(item="blanket", destination="2B", priority="routine", deadline_minutes=60))
    out = orch.handle_request(new_task(), "need a blanket")
    assert out.kind == "clarify" and "destination" in out.rows[0]["output"]["missing_fields"]


def test_a_named_destination_is_kept_even_in_words():
    orch = make_orch(pack=SIM6, override=model_says(item="wheelchair", destination="ward 3", priority="routine", deadline_minutes=60))
    out = orch.handle_request(new_task(), "Please bring a wheelchair to ward three.")
    assert out.kind != "clarify" and out.spec["destination_anchor"] == "south_room_approach"


def test_a_stated_deadline_alone_does_not_make_a_task_urgent():
    orch = make_orch(pack=SIM6, override=model_says(item="wheelchair", destination="2B", priority="urgent", deadline_minutes=15))
    out = orch.handle_request(new_task(), "Please bring a wheelchair to ward 2B within 15 minutes.")
    assert out.spec["priority"] == "routine" and out.spec["deadline_minutes"] == 15


def test_urgent_wording_always_makes_it_urgent():
    orch = make_orch(pack=SIM6, override=model_says(item="blanket", destination="2C", priority="routine", deadline_minutes=60))
    out = orch.handle_request(new_task(), "Blanket to 2C asap.")
    assert out.spec["priority"] == "urgent" and out.spec["deadline_minutes"] == 12
