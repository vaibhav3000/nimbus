"""C3 (no-LLM classical baseline) structured-form request path.

C3 takes requests through a deterministic web form instead of prose: the
fields are validated against the active pack/map and handed straight to the
classical scheduler/allocator, with no model call on the path. These tests
cover the orchestrator entry point (:meth:`Orchestrator.handle_structured_request`)
and the HTTP surface the requester app uses (``GET /ui-config`` and
``POST /requests`` with a ``form``).

They are additive and config-scoped: the orchestrator tests build a C3
orchestrator explicitly, and the HTTP tests stand up a C3 server; nothing
here changes C1/C2 behaviour.
"""

import threading

import pytest

import server
from conftest import http_json
from cloud_helpers import make_orch, new_task

# Facts of the pinned `hospital` pack (see conftest._pin_hospital_pack):
#   items        infusion_pump, monitor, iv_pole, ...
#   destinations 2A 2B 2C 3A ward-3 ward-5
#   sources      E-STORE W-STORE
#   priorities   urgent (default 12 min), routine (default 60 min)
#   deadline     1..480 minutes
VALID_FORM = {"item": "monitor", "destination": "2B", "priority": "routine", "deadline_minutes": 45}


# -- orchestrator entry point -----------------------------------------------------------------

def test_valid_form_leases_with_no_model_call():
    orch = make_orch(mode="C3")
    out = orch.handle_structured_request(new_task(), dict(VALID_FORM))
    assert out.kind == "leased"
    assert (out.spec["item"], out.spec["destination"], out.spec["priority"]) == ("monitor", "2B", "routine")
    assert out.spec["deadline_minutes"] == 45
    assert out.spec["rationale"] == "C3 structured form"
    assert out.lease is not None and out.lease["version"] == 1
    assert orch.llm.calls == []          # the whole point of C3: no LLM anywhere on the path


def test_deadline_defaults_to_the_priority_default_when_omitted():
    orch = make_orch(mode="C3")
    routine = orch.handle_structured_request(new_task(1), {"item": "monitor", "destination": "2B", "priority": "routine"})
    urgent = orch.handle_structured_request(new_task(2), {"item": "monitor", "destination": "2C", "priority": "urgent"})
    assert routine.spec["deadline_minutes"] == 60     # pack default for routine
    assert urgent.spec["deadline_minutes"] == 12      # pack default for urgent
    assert orch.llm.calls == []


def test_optional_source_is_validated_and_carried_into_the_spec():
    orch = make_orch(mode="C3")
    out = orch.handle_structured_request(new_task(), dict(VALID_FORM, source="E-STORE"))
    assert out.kind == "leased"
    assert out.spec["source"] == "E-STORE"


@pytest.mark.parametrize("n, form", [
    (1, dict(VALID_FORM)),                 # no source key at all
    (2, dict(VALID_FORM, source=None)),
    (3, dict(VALID_FORM, source="")),
])
def test_missing_source_is_allowed_and_leaves_no_source_in_the_spec(n, form):
    orch = make_orch(mode="C3")
    out = orch.handle_structured_request(new_task(n), form)
    assert out.kind == "leased" and "source" not in out.spec


@pytest.mark.parametrize("form, needle", [
    ({"item": "nope", "destination": "2B", "priority": "routine"}, "item"),
    ({"destination": "2B", "priority": "routine"}, "item"),                       # missing item
    ({"item": "monitor", "destination": "nowhere", "priority": "routine"}, "destination"),
    ({"item": "monitor", "destination": "E1", "priority": "routine"}, "destination"),   # real node, not a destination
    ({"item": "monitor", "priority": "routine"}, "destination"),                  # missing destination
    ({"item": "monitor", "destination": "2B", "priority": "express"}, "priority"),
    ({"item": "monitor", "destination": "2B"}, "priority"),                       # missing priority
    ({"item": "monitor", "destination": "2B", "priority": "routine", "source": "2A"}, "source"),   # not a source node
    ({"item": "monitor", "destination": "2B", "priority": "routine", "source": "nowhere"}, "source"),
])
def test_invalid_fields_are_rejected_before_any_model_call(form, needle):
    orch = make_orch(mode="C3")
    with pytest.raises(ValueError) as exc:
        orch.handle_structured_request(new_task(), form)
    assert needle in str(exc.value)
    assert orch.llm.calls == []


@pytest.mark.parametrize("deadline", [0, -5, 481, 10_000])
def test_deadline_out_of_the_pack_range_is_rejected(deadline):
    orch = make_orch(mode="C3")
    with pytest.raises(ValueError) as exc:
        orch.handle_structured_request(new_task(), dict(VALID_FORM, deadline_minutes=deadline))
    assert "deadline_minutes" in str(exc.value)


@pytest.mark.parametrize("deadline", [True, 30.0, "30", None])
def test_non_integer_deadline_is_rejected(deadline):
    orch = make_orch(mode="C3")
    with pytest.raises(ValueError) as exc:
        orch.handle_structured_request(new_task(), dict(VALID_FORM, deadline_minutes=deadline))
    assert "deadline_minutes" in str(exc.value)


@pytest.mark.parametrize("mode", ["C1", "C2"])
def test_structured_requests_are_refused_outside_c3(mode):
    orch = make_orch(mode=mode)
    with pytest.raises(ValueError) as exc:
        orch.handle_structured_request(new_task(), dict(VALID_FORM))
    assert "C3" in str(exc.value)


def test_form_is_not_a_dict_is_rejected():
    orch = make_orch(mode="C3")
    with pytest.raises(ValueError):
        orch.handle_structured_request(new_task(), ["not", "a", "dict"])


# -- HTTP surface -----------------------------------------------------------------------------

@pytest.fixture()
def c3_api(tmp_path, monkeypatch):
    """The whole server in C3 mode on an ephemeral port (pack pinned by conftest)."""
    monkeypatch.setenv("NIMBUS_MODE", "C3")
    httpd = server.make_server("127.0.0.1", 0, tmp_path / "data")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def test_ui_config_advertises_the_form_in_c3(c3_api):
    _, c3 = http_json("GET", c3_api + "/ui-config")
    assert c3["c3_structured_forms"] is True
    opts = c3["c3_form_options"]
    assert {"id": "2B", "label": "2B"} in opts["destinations"]
    assert {o["id"] for o in opts["items"]} >= {"monitor", "infusion_pump"}
    assert {o["id"] for o in opts["sources"]} == {"E-STORE", "W-STORE"}
    assert {p["id"]: p["deadline_minutes"] for p in opts["priorities"]} == {"urgent": 12, "routine": 60}


def test_ui_config_keeps_the_free_text_box_in_c1(api):
    _, c1 = http_json("GET", api + "/ui-config")          # default C1 server (no NIMBUS_MODE set)
    assert c1["c3_structured_forms"] is False


def test_post_form_creates_a_leased_task(c3_api):
    st, body = http_json("POST", c3_api + "/requests", {"form": dict(VALID_FORM), "message_id": "c3-ok"})
    assert st == 201 and body["status"] == "leased" and body["question"] is None
    _, task = http_json("GET", c3_api + f"/tasks/{body['task_id']}")
    assert task["spec"]["item"] == "monitor" and task["spec"]["destination"] == "2B"
    assert task["lease"]["cluster_id"] and task["lease"]["version"] == 1
    # the request was recorded as a structured form, not parsed prose
    timeline = http_json("GET", c3_api + f"/tasks/{body['task_id']}/timeline")[1]["timeline"]
    recv = next(e for e in timeline if e.get("output", {}).get("event") == "structured_request_received")
    assert recv["inputs"]["form"]["item"] == "monitor"


def test_post_form_with_a_bad_field_is_a_400(c3_api):
    st, body = http_json("POST", c3_api + "/requests", {"form": {"item": "nope", "destination": "2B", "priority": "routine"},
                                                        "message_id": "c3-bad"})
    assert st == 400 and "item" in body["error"]


def test_post_form_is_rejected_on_a_c1_server(api):
    st, body = http_json("POST", api + "/requests", {"form": dict(VALID_FORM), "message_id": "c1-form"})
    assert st == 409 and "C3" in body["error"]


def test_post_form_deduplicates_on_message_id(c3_api):
    st1, first = http_json("POST", c3_api + "/requests", {"form": dict(VALID_FORM), "message_id": "c3-dup"})
    st2, second = http_json("POST", c3_api + "/requests", {"form": dict(VALID_FORM), "message_id": "c3-dup"})
    assert st1 == 201 and first["duplicate"] is False
    assert st2 == 200 and second["duplicate"] is True        # replayed message makes no second task
    # exactly one task exists
    _, tasks = http_json("GET", c3_api + "/tasks")
    assert len(tasks["tasks"]) == 1
