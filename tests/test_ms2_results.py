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
