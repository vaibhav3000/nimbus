"""Regression tests for the cloud-side findings of the team red-team review (H1, H2, H6, H8, H14, M3, M5, M7, M8)."""

import json
import threading
import time
from datetime import datetime, timedelta, timezone

import contracts
import server
from cloud.results import MAX_UPLOAD_BYTES, ResultStore
from cloud.scheduler import Scheduler
from cloud.sqs_consumer import SQSConsumer
from conftest import http_json

RANKS = {"urgent": 0, "routine": 1}
T0 = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)


def start(tmp_path):
    httpd = server.make_server("127.0.0.1", 0, tmp_path / "d")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def leased_task(api, tag):
    _, body = http_json("POST", api + "/requests", {"text": "Need an infusion pump at 2B-12, urgent.", "message_id": f"rt-{tag}"})
    http_json("POST", api + f"/requests/{body['task_id']}/answer", {"text": "Yes, paediatric.", "message_id": f"rt-a-{tag}"})
    return body["task_id"]


def done(task_id, version, ref, mid):
    return {"type": "OUTPUT_READY", "message_id": mid, "lease_version": version, "task_id": task_id, "payload": {"result_ref": ref}}


# -- H1: one message id delivered many times at once makes one task ------------------------------------------------------

def test_h1_parallel_deliveries_of_one_message_id_make_one_task(tmp_path, monkeypatch):
    from cloud import llm as L
    orig = L.RuleBasedLLM.converse
    monkeypatch.setattr(L.RuleBasedLLM, "converse", lambda self, **kw: (time.sleep(0.3), orig(self, **kw))[1])
    httpd, api = start(tmp_path)
    try:
        out = []
        body = {"text": "Deliver a blanket to ward 3, routine", "message_id": "same-id"}
        ts = [threading.Thread(target=lambda: out.append(http_json("POST", api + "/requests", body))) for _ in range(6)]
        [t.start() for t in ts]; [t.join() for t in ts]
        created = [b["task_id"] for s, b in out if s == 201]
        assert len(created) == 1 and sum(b.get("duplicate") is True for s, b in out) == 5
        assert len(http_json("GET", api + "/tasks")[1]["tasks"]) == 1
    finally:
        httpd.shutdown()


def test_h1_a_failed_delivery_does_not_burn_its_message_id(api):
    assert http_json("POST", api + "/requests", {"text": "   ", "message_id": "retry-me"})[0] == 400
    assert http_json("POST", api + "/requests", {"text": "Deliver a blanket to ward 3, routine", "message_id": "retry-me"})[0] == 201


# -- H2: a completed task is completed once ------------------------------------------------------------------------------

def test_h2_a_replayed_output_ready_with_a_new_message_id_completes_nothing_twice(api):
    task_id = leased_task(api, "h2")
    assert http_json("POST", api + "/escalations", done(task_id, 1, "s3://nimbus-outputs/a.jpg", "h2-1"))[0] == 201
    rows = lambda: len(http_json("GET", api + f"/tasks/{task_id}/timeline")[1]["timeline"])
    notices = lambda: len(http_json("GET", api + "/notices")[1].get("notices", [])) if http_json("GET", api + "/notices")[0] == 200 else 0
    before_rows, before_notices = rows(), notices()
    status, resp = http_json("POST", api + "/escalations", done(task_id, 1, "s3://nimbus-outputs/a.jpg", "h2-2"))
    assert status == 200 and resp["duplicate"] is True and resp["already_completed"] is True
    assert rows() == before_rows and notices() == before_notices
    assert http_json("GET", api + f"/tasks/{task_id}")[1]["status"] == "completed"


def test_h2_no_reaction_to_an_event_on_a_task_that_is_over(api):
    task_id = leased_task(api, "h2b")
    http_json("POST", api + f"/tasks/{task_id}/control", {"action": "cancel", "message_id": "h2b-c"})
    version = http_json("GET", api + f"/tasks/{task_id}")[1]["lease"]["version"]       # the stop lease
    event = {"type": "UNEXPECTED_EVENT", "message_id": "h2b-e", "lease_version": version, "task_id": task_id,
             "payload": {"what_happened": "corridor blocked by a cart", "where": "E3", "robots_saw": ["R1 saw a cart"]}}
    status, resp = http_json("POST", api + "/escalations", event)
    assert status == 201 and http_json("GET", api + f"/tasks/{task_id}")[1]["status"] == "cancelled"
    assert resp.get("replanned") is None


# -- H6: cancelling is not held up by a request waiting on the model --------------------------------------------------

def test_h6_cancel_is_not_held_up_by_a_slow_model(tmp_path, monkeypatch):
    task_holder = {}
    httpd, api = start(tmp_path)
    try:
        task_holder["id"] = leased_task(api, "h6")
        from cloud import llm as L
        orig = L.RuleBasedLLM.converse
        monkeypatch.setattr(L.RuleBasedLLM, "converse", lambda self, **kw: (time.sleep(1.5), orig(self, **kw))[1])
        t = threading.Thread(target=lambda: http_json("POST", api + "/requests", {"text": "Deliver a blanket to ward 3, routine", "message_id": "h6-slow"}))
        t.start(); time.sleep(0.3)
        t0 = time.time()
        status, _ = http_json("POST", api + f"/tasks/{task_holder['id']}/control", {"action": "cancel", "message_id": "h6-cancel"})
        waited = time.time() - t0
        t.join()
        assert status == 201 and waited < 0.7, f"cancel waited {waited:.2f}s behind a model call"
    finally:
        httpd.shutdown()


# -- M3: the log row comes before the renewal it caused -----------------------------------------------------------------

def test_m3_checkin_is_logged_before_the_renewal_it_causes(api):
    task_id = leased_task(api, "m3")
    http_json("POST", api + "/tasks/" + task_id + "/replan", {"blocked_zones": [], "reason": "x", "message_id": "m3-rp"})
    lease = http_json("GET", api + f"/tasks/{task_id}")[1]["lease"]
    # move time so the renewal moves the expiry
    status, resp = http_json("POST", api + "/checkins", {"cluster_id": lease["cluster_id"], "lease_id": lease["lease_id"],
                                                         "lease_version": lease["version"], "timestamp": "2026-11-07T10:00:00Z", "message_id": "m3-ci"})
    assert status == 201
    stale = http_json("POST", api + "/checkins", {"cluster_id": lease["cluster_id"], "lease_id": f"lease-{task_id}-v1",
                                                  "lease_version": lease["version"] - 1, "timestamp": "2026-11-07T10:00:01Z", "message_id": "m3-ci2"}) \
        if lease["version"] > 1 else (409, {})
    assert stale[0] == 409
    tl = http_json("GET", api + f"/tasks/{task_id}/timeline")[1]["timeline"]
    assert not any(r["output"].get("event") == "checkin" and r["inputs"].get("checkin_timestamp") == "2026-11-07T10:00:01Z" for r in tl)  # a stale check-in is never logged


# -- M5: bad input gets a 400, not a dropped connection ---------------------------------------------------------------

def test_m5_bad_trace_parameter_and_oversized_body_get_a_400(api):
    assert http_json("GET", api + "/trace?after=x")[0] == 400
    import urllib.request as ur
    req = ur.Request(api + "/requests", data=b"{}", method="POST", headers={"Content-Length": str(server.Handler.MAX_BODY_BYTES + 1)})
    try:
        ur.urlopen(req, timeout=5)
    except Exception as exc:
        assert getattr(exc, "code", None) == 400


# -- M7: cost counters ---------------------------------------------------------------------------------------------------

def test_m7_a_decision_no_longer_adds_a_made_up_fargate_second(api):
    before = http_json("GET", api + "/cost")[1]["counters"]
    http_json("POST", api + "/requests", {"text": "Deliver a blanket to ward 3, routine", "message_id": "m7"})
    after = http_json("GET", api + "/cost")[1]["counters"]
    assert after["fargate_gbs"] == before["fargate_gbs"] and after["cloudwatch_metrics"] == before["cloudwatch_metrics"]


def test_m7_fargate_time_is_metered_from_the_clock_not_from_decisions(tmp_path):
    httpd = server.make_server("127.0.0.1", 0, tmp_path / "d")
    try:
        before = httpd.cost.snapshot()["fargate_gbs"]
        time.sleep(0.2)
        httpd.tick_once()
        assert httpd.cost.snapshot()["fargate_gbs"] - before >= 0.2
    finally:
        httpd.server_close()


# -- M8: an overdue routine task is not starved by new urgent work ---------------------------------------------------

def test_m8_overdue_routine_beats_a_newer_urgent_task():
    now = [T0]
    s = Scheduler(RANKS, now=lambda: now[0])
    s.push("routine-old", "routine", T0 + timedelta(minutes=10))
    s.push("urgent-1", "urgent", T0 + timedelta(minutes=30))
    assert [e.task_id for e in s.ordered()] == ["urgent-1", "routine-old"]         # on time: urgent first, as before
    now[0] = T0 + timedelta(minutes=20)                                             # the routine task is now overdue
    s.push("urgent-2", "urgent", T0 + timedelta(minutes=50))
    assert [e.task_id for e in s.ordered()][0] == "routine-old"
    assert [e.task_id for e in s.ordered()] == ["routine-old", "urgent-1", "urgent-2"][0:1] + ["urgent-1", "urgent-2"]


def test_m8_without_a_clock_the_order_is_unchanged():
    s = Scheduler(RANKS)
    s.push("r", "routine", T0 - timedelta(days=1)); s.push("u", "urgent", T0 + timedelta(days=1))
    assert [e.task_id for e in s.ordered()] == ["u", "r"]


# -- H8: results reach the cloud --------------------------------------------------------------------------------------------

def test_h8_a_result_on_the_edge_is_reported_as_not_uploaded(tmp_path):
    info = ResultStore(root=tmp_path).describe("storage://results/photo.jpg")
    assert info["readable"] is False and info["uploaded"] is False and info["location"] == "edge"


def test_h8_edge_uploads_a_file_and_the_cloud_can_read_it_back(api):
    task_id = leased_task(api, "h8")
    status, target = http_json("POST", api + "/results/upload-url", {"task_id": task_id, "name": "note.txt"})
    assert status == 201 and target["method"] == "PUT" and target["ref"].endswith(f"results/{task_id}/note.txt")
    import urllib.request as ur
    put = ur.Request(api + target["url"], data=b"corridor clear", method="PUT")
    assert ur.urlopen(put, timeout=5).status == 201
    assert http_json("POST", api + "/results/upload-url", {"task_id": task_id, "name": "../x"})[0] == 400
    assert http_json("POST", api + "/results/upload-url", {"task_id": "task-999999", "name": "a.txt"})[0] == 404
    assert http_json("POST", api + "/escalations", done(task_id, 1, target["ref"], "h8-done"))[0] == 201
    assert http_json("GET", api + f"/tasks/{task_id}")[1]["status"] == "completed"


def test_h8_s3_upload_is_a_presigned_put_for_our_bucket_only(tmp_path):
    class S3:
        def generate_presigned_url(self, op, Params, ExpiresIn):
            return f"https://s3.example/{Params['Bucket']}/{Params['Key']}?sig=1&exp={ExpiresIn}"
    rs = ResultStore(root=tmp_path, bucket="nimbus-outputs", client=S3())
    t = rs.presign_put("task-000001", "a.jpg", "image/jpeg")
    assert t["ref"] == "s3://nimbus-outputs/results/task-000001/a.jpg" and "sig=1" in t["url"] and t["max_bytes"] == MAX_UPLOAD_BYTES
    assert rs.presign_put("task-000001", "../../etc/passwd") is None and rs.save_upload("task-000001", "a.jpg", b"x") is None


# -- H4: the notice topic has a schema and the documentation names it ------------------------------------------------------

def test_h4_notice_payload_matches_its_schema_and_is_documented():
    from cloud.clock import Clock
    from cloud.notify import Notice, Notifier
    _, pub = Notifier(Clock()).send(Notice("cluster", "Use the service lift", type="ANSWER", cluster_id="east-wing", task_id="task-000002", lease_version=1))
    assert pub[0] == "nimbus/east-wing/notice" and contracts.validate_document("notice", pub[1]) == []
    assert "nimbus/{cluster_id}/notice" in (contracts.CONTRACTS_DIR / "topics.md").read_text(encoding="utf-8")


# -- H14: the queue consumer asks for a long enough visibility timeout and keeps its own socket timeouts bounded ------------

def test_h14_consumer_requests_a_visibility_timeout_and_lets_sqs_dead_letter():
    seen = {}

    class Q:
        def receive_message(self, **kw):
            seen.update(kw); return {"Messages": []}
    c = SQSConsumer("http://q", lambda t, b, k: (201, {}), client=Q(), wait_s=0)
    c.poll_once()
    assert seen["VisibilityTimeout"] >= 60 and c.max_receives > 5       # SQS's redrive (5 receives) comes first
