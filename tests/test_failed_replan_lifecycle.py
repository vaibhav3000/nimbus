"""What happens to a task when no re-plan is admissible (PRD FR-16, FR-22).

The designed answer is: the lease stays live and a human decides.  The cloud
escalates (PRD Table 2, UNEXPECTED EVENT -> "alert the user"), the operator
sees it on the console and pauses or cancels (FR-22), and `orch.cancel()` then
releases the robot, the zones and the reserved item.

These tests pin that contract from the `apply_outcome` side, which previously
had no coverage: an earlier attempt to auto-terminate on `replan_failed` passed
its own tests while silently breaking three others
(test_replan_with_no_route_is_409_and_keeps_v1,
test_both_gates_closed_gives_a_reason_and_alerts_the_operator_instead_of_looping,
test_no_route_left_alerts_the_operator).

KNOWN GAP, deliberately not fixed here: nothing bounds how long a task may sit
in this operator-pending state.  If nobody acts on the console, the robot and
the reserved item are held indefinitely -- there is no timeout, and
`replan_failed` is not in TERMINAL_STATUSES.  Whether that needs a backstop
(auto-cancel after N minutes, or an operator-console SLA) is a product
decision, not something to infer from the code.  See docs/RELEASE_TODO.md 3.2.
"""

import server
from cloud.orchestrator_types import Outcome
from cloud_helpers import make_orch, new_task
from cost import CostBook
from store import Store


def _apply(orch, store, task, outcome):
    server.apply_outcome(orch, store, CostBook(backend=None), lambda *a: None, task, outcome)


def _leased_task(orch, store):
    text = "Deliver a wheelchair to 2B-3, routine."
    task = new_task(1, text)
    out = orch.handle_request(task, text)
    assert out.kind == "leased", f"fixture needs a leased task, got {out.kind}"
    task.update(request_text=text)
    _apply(orch, store, task, out)
    assert task["status"] == "leased" and task["lease"]
    return task


def test_a_failed_replan_keeps_the_lease_so_the_operator_can_decide(tmp_path):
    """FR-22: the human makes the call, so the robot must still be under lease."""
    orch = make_orch()
    store = Store(tmp_path / "data")
    task = _leased_task(orch, store)
    version_before = task["lease"]["version"]

    _apply(orch, store, task, Outcome("replan_failed", reason="every route into 2B is closed"))

    assert task["status"] == "leased", "auto-terminating takes the decision away from the operator"
    assert task["lease"] is not None and task["lease"]["version"] == version_before
    assert orch.repo.current(task["task_id"]) is not None
    assert task["status"] not in server.TERMINAL_STATUSES


def test_operator_cancel_is_what_releases_the_robot_and_the_stock(tmp_path):
    """The release path exists and works -- it is reached by cancel, not by a failed re-plan."""
    orch = make_orch()
    store = Store(tmp_path / "data")
    task = _leased_task(orch, store)
    cluster, item = task["lease"]["cluster_id"], task["spec"]["item"]

    def stock_of():
        for c in orch.fleet.snapshot():
            if c["cluster_id"] == cluster:
                return c["stock"].get(item)
        return None

    stock_before = stock_of()
    _apply(orch, store, task, Outcome("replan_failed", reason="no route"))
    assert stock_of() == stock_before, "a failed re-plan must not move stock by itself"

    orch.cancel(task)

    assert orch.repo.current(task["task_id"]) is None
    assert task.get("stock_released") is True
    if stock_before is not None:
        assert stock_of() == stock_before + 1
