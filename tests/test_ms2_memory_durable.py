"""FR-17 / FR-19: state that survives a restart, and a place's habits used to stop asking."""

from cloud.durable import DynamoDurable, FileDurable, MemoryDurable
from cloud.mapstate import MapState
from cloud.memory import Memory
from cloud.notify import Notice, Notifier
from cloud.pack import Pack
from cloud.policy import PolicyBook
from cloud.topology import Topology
from cloud_helpers import make_orch, new_task


def test_closures_restrictions_and_notices_come_back_after_a_restart(tmp_path):
    pack = Pack.load("hospital"); topo = Topology(pack); orch = make_orch()
    d = FileDurable(tmp_path / "durable.json")
    m, pol, nt = MapState(topo, d), PolicyBook(pack, durable=d), Notifier(orch.clock, d)
    m.report("E3", "blocked", "east-wing", orch.clock.now(), ttl_s=900)
    pol.declare("2C", "isolation", "statement:task-000002")
    nt.send(Notice("operator", "isolation declared on 2C"))
    d2 = FileDurable(tmp_path / "durable.json")                     # a new process reads the same file
    m2, pol2, nt2 = MapState(topo, d2), PolicyBook(pack, durable=d2), Notifier(orch.clock, d2)
    assert "E3" in m2.closed(orch.clock.now()) and pol2.restrictions()["2C"]["kind"] == "isolation"
    assert [n["text"] for n in nt2.for_audience("operator")] == ["isolation declared on 2C"]
    pol.lift("2C"); m.clear("E3")
    d3 = FileDurable(tmp_path / "durable.json")
    assert PolicyBook(pack, durable=d3).restrictions() == {} and MapState(topo, d3).closed(orch.clock.now()) == {}


def test_a_damaged_file_does_not_stop_the_service(tmp_path):
    (tmp_path / "durable.json").write_text("{not json")
    assert FileDurable(tmp_path / "durable.json").load("closure") == {}


def test_orchestrator_from_env_persists_across_instances(tmp_path, monkeypatch):
    from cloud.orchestrator import Orchestrator
    monkeypatch.setenv("NIMBUS_PACK", "hospital")
    a = Orchestrator.from_env(tmp_path)
    a.policies.declare("2C", "isolation")
    b = Orchestrator.from_env(tmp_path)
    assert "2C" in b.policies.restrictions()


def test_robot_status_is_written_when_a_robot_changes_state_or_zone():
    orch = make_orch(); d = MemoryDurable(); orch.durable = d; orch.fleet.status_sink = d.put_robot
    orch.fleet.heartbeat("east-wing", {"robots": [{"id": "R1", "state": "busy", "zone": "E2", "pos": [1.0, 2.0]}]})
    orch.fleet.heartbeat("east-wing", {"robots": [{"id": "R1", "state": "busy", "zone": "E2"}]})      # unchanged: no write
    rec = d.load("robot")["R1"]
    assert rec["zone"] == "E2" and rec["state"] == "busy" and rec["cluster"] == "east-wing"


def test_dynamo_durable_round_trip_and_robot_table(monkeypatch):
    import pytest
    moto = pytest.importorskip("moto")
    import boto3
    from cloud.aws_dynamo import ensure_tables, tables_from_env
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(k, "x")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-south-1")
    with moto.mock_aws():
        c = boto3.client("dynamodb", region_name="ap-south-1"); ensure_tables(c, tables_from_env())
        d = DynamoDurable(c)
        d.put("closure", "E3", {"state": "blocked"}, ttl_s=600); d.put("restriction", "2C", {"kind": "isolation"})
        assert d.load("closure") == {"E3": {"state": "blocked"}} and d.load("restriction")["2C"]["kind"] == "isolation"
        d.delete("closure", "E3"); assert d.load("closure") == {}
        d.put_robot("R1", {"zone": "E2", "state": "busy"})
        assert c.get_item(TableName=tables_from_env()["robot_status"], Key={"robot_id": {"S": "R1"}})["Item"]["zone"]["S"] == '"E2"'


# -- FR-19: a place's habits ------------------------------------------------------------------------------------------------

def ask(orch, n, text="Need an infusion pump at 2B-12, urgent."):
    t = new_task(n, text); return t, orch.handle_request(t, text)


def test_after_two_confirmations_the_place_stops_being_asked_and_gets_the_usual_variant():
    orch = make_orch()
    for n in (1, 2):                                          # two requests at 2B, each clarified as paediatric
        t, q = ask(orch, n); assert q.kind == "clarify"
        out = orch.handle_request(t, t["request_text"], question=q.question, answer="Yes, a paediatric pump please.")
        assert out.kind == "leased" and out.spec["item"] == "infusion_pump_paediatric"
        orch.finish({"task_id": t["task_id"], "reservation": out.reservation, "spec": out.spec, "lease": out.lease})
    orch.fleet.set_stock("east-wing", {"infusion_pump_paediatric": 5})    # the first two consumed the stock
    t, out = ask(orch, 3)
    assert out.kind == "leased" and out.spec["item"] == "infusion_pump_paediatric"      # no question this time
    assert any("usual choice" in c for c in out.spec["constraints"])
    assert any("Usual for 2B" in l for l in orch.memory.lines_for("2B"))


def test_one_confirmation_is_not_enough_and_an_explicit_standard_request_beats_memory():
    orch = make_orch()
    t, q = ask(orch, 1); orch.handle_request(t, t["request_text"], question=q.question, answer="Yes, paediatric.")
    assert ask(orch, 2)[1].kind == "clarify"                  # only one confirmation so far
    for n in (3, 4):
        t, q = ask(orch, n)
        o = orch.handle_request(t, t["request_text"], question=q.question, answer="Yes, paediatric.")
        orch.finish({"task_id": t["task_id"], "reservation": o.reservation, "spec": o.spec, "lease": o.lease})
    assert orch.memory.usual("2B", "infusion_pump") == "infusion_pump_paediatric"
    t, q = ask(orch, 9, "Need a standard infusion pump at 2B-12, urgent. No, standard is fine.")
    assert q.kind == "leased" and q.spec["item"] == "infusion_pump" and q.spec["variant_confirmed_by_requester"] is True


def test_memory_never_overrides_a_safety_rule():
    m = Memory()
    for _ in range(3):
        m.record("2B", "infusion_pump", "infusion_pump")        # people kept confirming "standard" for a paediatric place
    assert m.usual("2B", "infusion_pump") is None               # the base item is never "remembered" as a shortcut
    orch = make_orch(); orch.memory = m
    assert ask(orch, 1)[1].kind == "clarify"                     # the paediatric rule still asks


def test_memory_survives_a_restart(tmp_path):
    d = FileDurable(tmp_path / "d.json"); a = Memory(d)
    for _ in range(2):
        a.record("2B", "infusion_pump", "infusion_pump_paediatric")
    assert Memory(FileDurable(tmp_path / "d.json")).usual("2B", "infusion_pump") == "infusion_pump_paediatric"
