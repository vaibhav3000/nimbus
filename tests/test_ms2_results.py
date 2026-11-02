"""S3 / result handling: what the cloud reads, what it tells the requester, what it archives."""

from pathlib import Path

from cloud.results import ResultStore
from cloud_helpers import make_orch, new_task
from conftest import http_json


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.heads = []

    def put_object(self, Bucket, Key, Body, ContentType):
        self.objects[(Bucket, Key)] = (Body, ContentType)

    def head_object(self, Bucket, Key):
        self.heads.append((Bucket, Key))
        body, ct = self.objects[(Bucket, Key)]
        return {"ContentLength": len(body), "ContentType": ct}

    def get_object(self, Bucket, Key, Range=None):
        import io
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)][0][:4000])}


def test_s3_store_reads_only_its_own_bucket_and_gives_a_short_preview():
    s3 = FakeS3(); store = ResultStore(bucket="mine", client=s3)
    s3.put_object("mine", "results/t1/list.json", b'{"pumps": 3}', "application/json")
    s3.put_object("mine", "results/t1/proof.jpg", b"\xff" * 5000, "image/jpeg")
    a = store.describe("s3://mine/results/t1/list.json")
    assert a["readable"] and a["preview"] == '{"pumps": 3}' and a["size"] == 12
    assert "preview" not in store.describe("s3://mine/results/t1/proof.jpg") and store.describe("s3://mine/results/t1/proof.jpg")["size"] == 5000
    assert store.describe("s3://someone-elses/secret")["readable"] is False and ("someone-elses", "secret") not in s3.heads
    assert store.describe("s3://mine/../x")["readable"] is False
    assert store.describe("s3://mine/results/missing")["readable"] is False       # never raises


def test_local_store_cannot_be_walked_out_of_its_folder(tmp_path):
    root = tmp_path / "results"; root.mkdir()
    (root / "a.txt").write_text("hello"); (tmp_path / "secret.txt").write_text("nope")
    store = ResultStore(root=root)
    assert store.describe("storage://a.txt")["preview"] == "hello"
    assert store.describe("storage://../secret.txt")["readable"] is False


def test_put_returns_a_reference_and_failures_return_none(tmp_path):
    assert ResultStore(root=tmp_path).put_json("logs/x.json", {"a": 1}) == "file://logs/x.json"
    assert (tmp_path / "logs" / "x.json").exists()

    class Boom(FakeS3):
        def put_object(self, **kw): raise RuntimeError("AccessDenied")
    assert ResultStore(bucket="b", client=Boom()).put_json("k", {}) is None


def test_completed_task_gets_a_template_summary_for_a_photo_and_a_model_summary_for_a_text_result(tmp_path):
    orch = make_orch(); orch.results = ResultStore(root=tmp_path)
    (tmp_path / "r").mkdir(); (tmp_path / "r" / "list.txt").write_text("3 idle pumps: 2A, 2B, E-STORE")
    t = new_task(1, "x"); t.update(spec={"item": "wheelchair", "destination": "ward-3"}, lease={"version": 1})
    photo = orch.complete_summary(t, "storage://r/proof.jpg")
    assert photo["calls"] == 0 and "wheelchair" in photo["text"] and photo["rows"][0]["who"] == "rule"
    text = orch.complete_summary(t, "storage://r/list.txt")
    assert text["calls"] == 1 and "3 idle pumps" in text["text"] and text["rows"][0]["who"] == "cloud"


def test_c3_never_calls_a_model_for_the_summary(tmp_path):
    orch = make_orch(mode="C3"); orch.results = ResultStore(root=tmp_path)
    (tmp_path / "list.txt").write_text("3 idle pumps")
    t = new_task(1, "x"); t.update(spec={"item": "pump", "destination": "2A"}, lease={"version": 1})
    assert orch.complete_summary(t, "storage://list.txt")["calls"] == 0 and orch.llm.calls == []


def test_over_http_a_finished_task_has_a_summary_a_notice_and_an_archive(api):
    _, b = http_json("POST", api + "/requests", {"text": "Deliver a wheelchair to ward 3, routine.", "message_id": "rs1"})
    tid = b["task_id"]
    http_json("POST", api + "/escalations", {"type": "OUTPUT_READY", "message_id": "rs-o", "lease_version": 1, "task_id": tid,
                                            "payload": {"result_ref": "s3://nimbus-outputs/x/proof.jpg"}})
    t = http_json("GET", api + f"/tasks/{tid}")[1]
    assert t["status"] == "completed" and "wheelchair" in t["summary"] and t["archive_ref"] == f"file://logs/{tid}.json"
    assert any(tid in n["text"] for n in http_json("GET", api + "/notifications?audience=requester")[1]["notifications"])
    _, tl = http_json("GET", api + f"/tasks/{tid}/timeline")
    assert any(r["output"].get("decision") == "summarise_result" for r in tl["timeline"])


# -- the lease bundle (data and rules the edge needs, beside the unchanged lease) -----------------------------------------------

def test_every_lease_comes_with_a_small_bundle_on_the_notice_topic(api):
    import json
    _, b = http_json("POST", api + "/requests", {"text": "Deliver a wheelchair to 2B-3, routine.", "message_id": "bd1"})
    out = http_json("GET", api + "/outbox")[1]["outbox"]
    lease = next(r for r in out if r["topic"].endswith("/lease"))
    bundle = next(r for r in out if r["topic"] == "nimbus/east-wing/notice" and r["payload"].get("type") == "BUNDLE")["payload"]
    assert bundle["lease_id"] == lease["payload"]["lease_id"] and bundle["lease_version"] == 1
    m = bundle["bundle"]
    assert m["map"]["nodes"] and set(m["map"]["nodes"]) <= set(lease["payload"]["allowed_zones"]) and m["guidance"]["escalate_when"] == lease["payload"]["when_to_escalate"]
    assert m["map"]["edges"] and len(json.dumps(bundle)) <= 4600
    assert set(lease["payload"]) == {"lease_id", "version", "priority", "deadline_ts", "expiry_ts", "allowed_zones", "forbidden_zones",
                                     "speed_limit", "assigned_robots", "task_sequence", "cluster_id", "when_to_escalate"}   # still 12 fields


def test_bundle_carries_rules_restrictions_and_closures_in_force():
    from cloud.bundle import build_bundle
    orch = make_orch()
    orch.policies.declare("2A", "isolation"); orch.mapstate.report("E3", "blocked", "east-wing", orch.clock.now())
    t = new_task(1, "x"); out = orch.handle_request(t, "Deliver a wheelchair to 2B-3, routine.")
    b = build_bundle(orch, out.lease, out.spec)
    assert b["restrictions"].get("2A", {}).get("kind") == "isolation" or "2A" not in out.lease["allowed_zones"]
    assert "E3" not in b["map"]["nodes"] or b["closed"].get("E3") == "blocked"
    assert any(r["id"] for r in b["rules"])


def test_bundle_is_stored_in_s3_when_a_bucket_is_configured_and_can_be_switched_off(monkeypatch):
    orch = make_orch(); s3 = FakeS3(); orch.results = ResultStore(bucket="mine", client=s3)
    t = new_task(1, "x"); out = orch.handle_request(t, "Deliver a wheelchair to 2B-3, routine.")
    topic, msg = out.publish[1]
    assert msg["bundle_ref"] == f"s3://mine/bundles/{out.lease['lease_id']}.json" and ("mine", f"bundles/{out.lease['lease_id']}.json") in s3.objects
    monkeypatch.setenv("NIMBUS_BUNDLES", "0")
    assert len(make_orch().handle_request(new_task(2, "x"), "Deliver a wheelchair to 2B-3, routine.").publish) == 1
