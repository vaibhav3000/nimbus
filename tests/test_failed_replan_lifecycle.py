"""TODO 3.2 regression: a failed re-plan must release the task, not strand it.

`replan()`/`reassign()` return Outcome("replan_failed", ...) when neither the
lease's own cluster nor any other can finish the work.  Before the fix,
`apply_outcome` had no branch for that kind, so the outcome fell through every
elif and the task stayed `leased` with its robot and its reserved item held for
good (PRD FR-16; Fig. 3: "the cloud ... always reports the final result or
failure back to the user").
"""

import server
from cloud.orchestrator_types import Outcome
from cloud_helpers import make_orch, new_task
from cost import CostBook
from store import Store


def _apply(orch, store, task, outcome):
    server.apply_outcome(orch, store, CostBook(backend=None), lambda *a: None, task, outcome)


def _stock_of(orch, cluster, item):
    for c in orch.fleet.snapshot():
        if c["cluster_id"] == cluster:
            return c["stock"].get(item)
    return None


def _robot_state(orch, robot_id):
    for c in orch.fleet.snapshot():
        for r in c["robots"]:
            if r["id"] == robot_id:
                return r.get("state")
    return None


def _leased_task(orch, store):
    """A task holding a live lease, a robot and a reserved item."""
    text = "Deliver a wheelchair to 2B-3, routine."
    task = new_task(1, text)
    out = orch.handle_request(task, text)
    assert out.kind == "leased", f"fixture needs a leased task, got {out.kind}"
    task.update(request_text=text)
    _apply(orch, store, task, out)
    assert task["status"] == "leased" and task["lease"]
    return task


def test_failed_replan_releases_the_task_its_robot_and_its_stock(tmp_path):
    orch = make_orch()
    store = Store(tmp_path / "data")
    task = _leased_task(orch, store)

    robot = task["lease"]["assigned_robots"][0]
    cluster = task["lease"]["cluster_id"]
    item = task["spec"]["item"]
    stock_before = _stock_of(orch, cluster, item)

    _apply(orch, store, task, Outcome("replan_failed", reason="every route into 2B is closed"))

    # Terminal, with the reason kept for the operator and the timeline.
    assert task["status"] == "failed"
    assert "every route into 2B is closed" in task["reason"]
    assert task["status"] in server.TERMINAL_STATUSES

    # The lease is closed and the robot can take other work.
    assert task["lease"] is None
    assert orch.repo.current(task["task_id"]) is None
    assert _robot_state(orch, robot) != "busy"

    # The item goes back on the shelf: the delivery never happened.
    if stock_before is not None:
        assert _stock_of(orch, cluster, item) == stock_before + 1
    assert task.get("stock_released") is True


def test_failed_replan_tells_the_requester_why(tmp_path):
    orch = make_orch()
    store = Store(tmp_path / "data")
    task = _leased_task(orch, store)

    before = len(orch.notifier.for_audience("requester"))
    _apply(orch, store, task, Outcome("replan_failed", reason="west-wing has gone silent"))

    notices = orch.notifier.for_audience("requester")
    assert len(notices) > before, "the requester must be told the task failed (PRD Fig. 3)"
    assert "west-wing has gone silent" in notices[-1]["text"]


def test_a_failed_task_is_not_retried_by_the_queue_drain(tmp_path):
    """Terminal means terminal: the scheduler must not pick it up again."""
    orch = make_orch()
    store = Store(tmp_path / "data")
    task = _leased_task(orch, store)

    _apply(orch, store, task, Outcome("replan_failed", reason="no route"))
    retried = orch.drain(lambda tid: task if tid == task["task_id"] else None)
    assert [t["task_id"] for t, _ in retried] != [task["task_id"]]
