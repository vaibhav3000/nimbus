"""AWS adapters, tested on moto (no account): DynamoDB lease repo + store, and
the Bedrock Converse adapter against a stub client."""

import json
import random
import threading
from datetime import timedelta

import pytest

moto = pytest.importorskip("moto")
import boto3  # noqa: E402

from cloud.aws_dynamo import DynamoLeaseRepo, ensure_tables, tables_from_env  # noqa: E402
from cloud.aws_store import DynamoStore  # noqa: E402
from cloud.clock import FixedClock, iso  # noqa: E402
from cloud.leases import Conflict, MemoryLeaseRepo  # noqa: E402
from cloud.llm import BedrockLLM, CassetteLLM, LLMError, LLMResult  # noqa: E402
from cloud.pack import Pack  # noqa: E402
from cloud.tools import ToolRegistry  # noqa: E402

REGISTRY = ToolRegistry(Pack.load())
from cloud_helpers import make_orch, new_task  # noqa: E402
from test_cloud_units import lease  # noqa: E402


@pytest.fixture()
def ddb(monkeypatch):
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(k, "x")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-south-1")
    with moto.mock_aws():
        client = _SerializedClient(boto3.client("dynamodb", region_name="ap-south-1"))
        names = tables_from_env()
        ensure_tables(client._inner, names)
        yield client, names


class _SerializedClient:
    """moto is not thread-safe (see conftest); real DynamoDB is. Serialize calls."""
    _lock = threading.RLock()

    def __init__(self, inner):
        self._inner, self.exceptions = inner, inner.exceptions

    def __getattr__(self, name):
        fn = getattr(self._inner, name)

        def call(*a, **kw):
            with self._lock:
                return fn(*a, **kw)
        return call if callable(fn) else fn


@pytest.fixture(params=["memory", "dynamodb"])
def repo(request, ddb):
    return MemoryLeaseRepo() if request.param == "memory" else DynamoLeaseRepo(*ddb)


# -- repo parity: the same behaviour from both implementations --------------------------

def test_one_holder_and_expiry_frees_it(repo):
    clock = FixedClock()
    exp = iso(clock.now() + timedelta(minutes=5))
    repo.commit(lease("task-000001", 1, ["R1"], exp), ["robot:R1"], now=clock.now())
    with pytest.raises(Conflict):
        repo.commit(lease("task-000002", 1, ["R1"], exp), ["robot:R1"], now=clock.now())
    assert repo.holder("robot:R1", clock.now())["task_id"] == "task-000001"
    clock.advance(301)
    assert repo.holder("robot:R1", clock.now()) is None
    repo.commit(lease("task-000002", 1, ["R1"], exp), ["robot:R1"], now=clock.now())


def test_versions_plus_one_claims_move_and_release(repo):
    clock = FixedClock()
    exp = iso(clock.now() + timedelta(minutes=5))
    repo.commit(lease("task-000001", 1, ["R1"], exp, zones=["W0"]), ["robot:R1", "zone:W0"], now=clock.now())
    with pytest.raises(Conflict):
        repo.commit(lease("task-000001", 3, ["R1"], exp), ["robot:R1"], now=clock.now())
    repo.commit(lease("task-000001", 2, ["R2"], exp), ["robot:R2"], now=clock.now())
    assert repo.holder("robot:R1", clock.now()) is None and repo.holder("zone:W0", clock.now()) is None
    assert repo.current("task-000001")["version"] == 2
    assert sorted(repo.release("task-000001")) == ["robot:R2"]
    assert repo.current("task-000001") is None
    assert repo.release("task-nope") == []


def test_exclusive_zone_is_single_lane(repo):
    clock = FixedClock()
    exp = iso(clock.now() + timedelta(minutes=5))
    repo.commit(lease("task-000001", 1, ["R1"], exp), ["robot:R1", "zone:W0"], now=clock.now())
    with pytest.raises(Conflict) as exc:
        repo.commit(lease("task-000002", 1, ["R2"], exp), ["robot:R2", "zone:W0"], now=clock.now())
    assert any("W0" in r for r in exc.value.reasons)
    assert repo.holder("robot:R2", clock.now()) is None  # all-or-nothing: R2 was not claimed either


def test_renew_extends_never_shortens_and_checks_version(repo):
    clock = FixedClock()
    exp = iso(clock.now() + timedelta(minutes=5))
    later = iso(clock.now() + timedelta(minutes=9))
    repo.commit(lease("task-000001", 1, ["R1"], exp), ["robot:R1"], now=clock.now())
    assert repo.renew("task-000001", 1, later, now=clock.now())["expiry_ts"] == later
    assert repo.holder("robot:R1", clock.now())["until"] == later
    assert repo.renew("task-000001", 1, exp, now=clock.now())["expiry_ts"] == later
    with pytest.raises(Conflict):
        repo.renew("task-000001", 2, later, now=clock.now())


def test_lease_lab_random_interleavings(repo):
    rng = random.Random(11)
    clock = FixedClock()
    versions = {}
    for _ in range(250):
        task = f"task-{rng.randint(1, 5):06d}"
        robot = f"R{rng.randint(1, 3)}"
        op = rng.choice(["new", "replace", "renew", "release", "tick"])
        exp = iso(clock.now() + timedelta(minutes=rng.randint(1, 10)))
        try:
            cur = repo.current(task)
            if op == "new" and cur is None and task not in versions:
                repo.commit(lease(task, 1, [robot], exp), [f"robot:{robot}"], now=clock.now())
                versions[task] = 1
            elif op == "replace" and cur:
                repo.commit(lease(task, cur["version"] + 1, [robot], exp), [f"robot:{robot}"], now=clock.now())
            elif op == "renew" and cur:
                repo.renew(task, cur["version"], exp, now=clock.now())
            elif op == "release":
                repo.release(task)
            elif op == "tick":
                clock.advance(rng.randint(1, 240))
        except Conflict:
            pass
        for r in ("R1", "R2", "R3"):
            h = repo.holder(f"robot:{r}", clock.now())
            if h:
                cur = repo.current(h["task_id"])
                assert cur and r in cur["assigned_robots"] and cur["version"] == h["version"]


# -- races: only the DynamoDB repo has real read-then-write windows -------------------------

def test_racing_commits_for_one_robot_have_exactly_one_winner(ddb):
    repo = DynamoLeaseRepo(*ddb)
    now = FixedClock().now()
    exp = iso(now + timedelta(minutes=5))
    results, barrier = [], threading.Barrier(12)

    def go(i):
        barrier.wait()
        try:
            repo.commit(lease(f"task-{i:06d}", 1, ["R1"], exp), ["robot:R1"], now=now)
            results.append("won")
        except Conflict:
            results.append("lost")

    ts = [threading.Thread(target=go, args=(i,)) for i in range(1, 13)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert results.count("won") == 1 and results.count("lost") == 11


def test_racing_replacements_of_one_task_move_the_version_once(ddb):
    repo = DynamoLeaseRepo(*ddb)
    now = FixedClock().now()
    exp = iso(now + timedelta(minutes=5))
    repo.commit(lease("task-000001", 1, ["R1"], exp), ["robot:R1"], now=now)
    wins, barrier = [], threading.Barrier(8)

    def go():
        barrier.wait()
        try:
            repo.commit(lease("task-000001", 2, ["R1"], exp), ["robot:R1"], now=now)
            wins.append(1)
        except Conflict:
            pass

    ts = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert len(wins) == 1 and repo.current("task-000001")["version"] == 2


def test_history_table_keeps_every_issued_lease(ddb):
    client, names = ddb
    repo = DynamoLeaseRepo(client, names)
    now = FixedClock().now()
    exp = iso(now + timedelta(minutes=5))
    repo.commit(lease("task-000001", 1, ["R1"], exp), ["robot:R1"], now=now)
    repo.commit(lease("task-000001", 2, ["R1"], exp), ["robot:R1"], now=now)
    items = client.scan(TableName=names["leases"])["Items"]
    assert sorted(i["lease_id"]["S"] for i in items) == ["lease-task-000001-v1", "lease-task-000001-v2"]


# -- DynamoStore -----------------------------------------------------------------------------------

def test_store_task_ids_are_unique_under_concurrency(ddb):
    store = DynamoStore(*ddb)
    ids = []
    ts = [threading.Thread(target=lambda: ids.append(store.next_task_id())) for _ in range(20)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert len(set(ids)) == 20 and "task-000001" in ids


def test_store_message_id_claim_is_atomic(ddb):
    store = DynamoStore(*ddb)
    assert "m1" not in store.seen_message_ids
    wins = []
    ts = [threading.Thread(target=lambda: wins.append(store.see_message_id("m1"))) for _ in range(10)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert wins.count(True) == 1 and "m1" in store.seen_message_ids


def test_store_roundtrip_timeline_order_and_outbox(ddb):
    store = DynamoStore(*ddb)
    task = {"task_id": store.next_task_id(), "status": "leased", "message_id": "m9", "lease": {"version": 1}}
    store.put_task(task)
    assert store.get_task(task["task_id"]) == task and "m9" in store.seen_message_ids
    for i in range(3):
        store.append_decision({"task_id": task["task_id"], "timestamp": "2026-10-18T09:00:00Z",
                               "who": "cloud", "inputs": {}, "output": {"n": i}, "lease_version": None})
    assert [r["output"]["n"] for r in store.timeline(task["task_id"])] == [0, 1, 2]
    store.append_outbox({"topic": "nimbus/east-wing/lease", "payload": {"a": 1}})
    assert store.outbox()[0]["topic"] == "nimbus/east-wing/lease"
    assert [t["task_id"] for t in store.all_tasks()] == [task["task_id"]]


def test_store_publishes_to_iot_when_configured(ddb):
    sent = []

    class FakeIot:
        def publish(self, **kw):
            sent.append(kw)

    store = DynamoStore(*ddb, iot_client=FakeIot())
    store.append_outbox({"topic": "nimbus/east-wing/lease", "payload": {"v": 1}})
    assert sent[0]["topic"] == "nimbus/east-wing/lease" and sent[0]["qos"] == 1
    assert json.loads(sent[0]["payload"]) == {"v": 1}


def test_orchestrator_runs_sc1_on_the_dynamodb_repo(ddb):
    orch = make_orch(repo=DynamoLeaseRepo(*ddb))
    task = new_task()
    q = orch.handle_request(task, "Need an infusion pump at 2B-12, urgent.")
    out = orch.handle_request(task, "Need an infusion pump at 2B-12, urgent.", question=q.question, answer="paediatric")
    assert out.kind == "leased"
    task.update(lease=out.lease, spec=out.spec)
    assert orch.repo.holder("robot:R1", orch.clock.now())["task_id"] == task["task_id"]
    assert orch.replan(task, blocked_zones=["E3"], reason="x").lease["version"] == 2


# -- Bedrock Converse adapter ----------------------------------------------------------------------------

class StubBedrock:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.requests = response, error, []

    def converse(self, **kw):
        self.requests.append(kw)
        if self.error:
            raise self.error
        return self.response


def converse_reply(name, payload, tin=321, tout=45):
    return {"output": {"message": {"role": "assistant", "content": [
        {"text": "thinking"}, {"toolUse": {"toolUseId": "t1", "name": name, "input": payload}}]}},
        "usage": {"inputTokens": tin, "outputTokens": tout}, "stopReason": "tool_use"}


def test_bedrock_adapter_sends_a_converse_request_and_parses_tool_use():
    stub = StubBedrock(converse_reply("ask_clarification", {"question": "q?", "possible_readings": ["a"]}))
    llm = BedrockLLM("apac.test-model-v1:0", REGISTRY, client=stub)
    res = llm.converse(system="SYS", messages=[{"role": "user", "text": "hi"}, {"role": "assistant", "text": "q"}],
                       tools=["submit_task_spec", "ask_clarification"])
    req = stub.requests[0]
    assert req["modelId"] == "apac.test-model-v1:0" and req["system"] == [{"text": "SYS"}]
    assert req["messages"][0] == {"role": "user", "content": [{"text": "hi"}]}
    assert [t["toolSpec"]["name"] for t in req["toolConfig"]["tools"]] == ["submit_task_spec", "ask_clarification"]
    assert req["toolConfig"]["toolChoice"] == {"any": {}}
    assert req["inferenceConfig"]["temperature"] == 0.0
    assert res.tool_calls == [{"name": "ask_clarification", "input": {"question": "q?", "possible_readings": ["a"]}}]
    assert (res.input_tokens, res.output_tokens, res.estimated) == (321, 45, False)
    assert res.text == "thinking"


def test_bedrock_failure_becomes_llm_error_so_fallback_engages():
    llm = BedrockLLM("m", REGISTRY, client=StubBedrock(error=RuntimeError("ThrottlingException")))
    with pytest.raises(LLMError, match="ThrottlingException"):
        llm.converse(system="s", messages=[{"role": "user", "text": "x"}], tools=["propose_allocation"])


def test_orchestrator_with_bedrock_shaped_model_uses_real_usage_and_falls_back_on_throttle():
    orch = make_orch()
    spec = {"item": "wheelchair", "destination": "ward-3", "priority": "routine", "deadline_minutes": 60, "rationale": "r"}

    class Seq(StubBedrock):
        def converse(self, **kw):
            self.requests.append(kw)
            name = kw["toolConfig"]["tools"][0]["toolSpec"]["name"]
            if name == "submit_task_spec":
                return converse_reply("submit_task_spec", spec, 900, 60)
            raise RuntimeError("ThrottlingException")  # allocation call is throttled

    orch.llm = BedrockLLM("m", orch.tools, client=Seq())
    out = orch.handle_request(new_task(), "wheelchair to ward 3")
    assert out.kind == "leased" and "Reduced autonomy" in out.notice   # intent by model, allocation by rules
    assert (out.tokens_in, out.tokens_out) == (900, 60)                # real usage, not an estimate
    assert out.rows[0]["output"]["llm"]["tokens_estimated"] is False


def test_tool_schemas_are_valid_json_schema():
    from jsonschema import Draft202012Validator
    for name in REGISTRY.names():
        Draft202012Validator.check_schema(REGISTRY.schema(name))
    cfg = REGISTRY.bedrock_config(["propose_allocation"], force=False)
    assert "toolChoice" not in cfg and cfg["tools"][0]["toolSpec"]["inputSchema"]["json"]["required"]


# -- cassette ------------------------------------------------------------------------------------------------------

def test_cassette_records_once_replays_free_and_flags_changed_prompts(tmp_path):
    stub = StubBedrock(converse_reply("propose_allocation", {"cluster_id": "east-wing", "robot_ids": ["R1"], "rationale": "r"}))
    path = tmp_path / "tape.json"
    kw = dict(system="S", messages=[{"role": "user", "text": "m"}], tools=["propose_allocation"])
    rec = CassetteLLM(BedrockLLM("m", REGISTRY, client=stub), path, mode="record")
    first = rec.converse(**kw)
    assert len(stub.requests) == 1
    replay = CassetteLLM(None, path, mode="replay")
    again = replay.converse(**kw)
    assert again.tool_calls == first.tool_calls and again.input_tokens == 321 and len(stub.requests) == 1
    with pytest.raises(LLMError, match="cassette miss"):
        replay.converse(**dict(kw, system="S changed"))


def test_cassette_key_ignores_the_clock_but_not_the_prompt(tmp_path):
    stub = StubBedrock(converse_reply("propose_allocation", {"cluster_id": "east-wing", "robot_ids": ["R1"], "rationale": "r"}))
    path = tmp_path / "tape.json"
    mk = lambda ts, extra="": dict(system=f"CONTEXT (as of {ts}, mode C1)" + extra,
                                   messages=[{"role": "user", "text": "m"}], tools=["propose_allocation"])
    CassetteLLM(BedrockLLM("m", REGISTRY, client=stub), path, mode="record").converse(**mk("2026-10-18T09:00:00Z"))
    replay = CassetteLLM(None, path, mode="replay")
    assert replay.converse(**mk("2026-10-19T17:45:10Z")).tool_calls  # a later run still replays
    with pytest.raises(LLMError):
        replay.converse(**mk("2026-10-19T17:45:10Z", " plus new rule"))
