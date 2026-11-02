"""NFR-5 edge token, metrics, request-to-lease timing."""

import json
from urllib import request as urlreq
from urllib.error import HTTPError

from conftest import http_json


def raw(method, url, body=None, token=None):
    req = urlreq.Request(url, data=None if body is None else json.dumps(body).encode(), method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urlreq.urlopen(req) as r:
            return r.status
    except HTTPError as e:
        return e.code


def test_edge_endpoints_need_the_token_when_one_is_configured_and_nothing_else_does(api, monkeypatch):
    monkeypatch.setenv("NIMBUS_EDGE_TOKEN", "s3cret-token")
    hb = {"cluster_id": "east-wing"}
    assert raw("POST", api + "/fleet/heartbeat", hb) == 401
    assert raw("POST", api + "/fleet/heartbeat", hb, token="wrong") == 401
    assert raw("POST", api + "/fleet/heartbeat", hb, token="s3cret-token") == 201
    assert raw("GET", api + "/outbox") == 401 and raw("GET", api + "/outbox", token="s3cret-token") == 200
    assert raw("POST", api + "/escalations", {}, ) == 401 and raw("POST", api + "/checkins", {}) == 401
    # user-facing routes are not edge routes (API Gateway protects those in the deployed system)
    assert raw("GET", api + "/map.json") == 200 and raw("POST", api + "/requests", {"text": "wheelchair to 2B-3", "message_id": "au1"}) == 201


def test_no_token_configured_means_open_as_before(api):
    assert raw("POST", api + "/fleet/heartbeat", {"cluster_id": "east-wing"}) == 201


def test_fake_edge_sends_the_token(api, monkeypatch):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from edge_sim import EdgeSim
    monkeypatch.setenv("NIMBUS_EDGE_TOKEN", "tok")
    assert EdgeSim(api, "east-wing").heartbeat()[0] == 201


def test_metrics_report_request_to_lease_time_model_calls_and_gauges(api):
    http_json("POST", api + "/requests", {"text": "Deliver a wheelchair to 2B-3, routine.", "message_id": "mt1"})
    http_json("POST", api + "/requests", {"text": "Need an infusion pump at 2B-12, urgent.", "message_id": "mt2"})
    m = http_json("GET", api + "/metrics")[1]
    assert m["counters"]["requests"] == 2 and m["counters"]["outcome_leased"] == 1 and m["counters"]["outcome_clarify"] == 1
    assert m["latency"]["request_to_lease_s"]["n"] == 1 and m["latency"]["request_to_lease_s"]["p95"] < 5
    assert m["counters"]["llm_calls"] == 2 and m["latency"]["llm_latency_ms"]["n"] == 2
    assert m["gauges"]["live_leases"] == 1 and m["gauges"]["clusters"]["east-wing"] and "tasks_by_status" in m["gauges"]


def test_percentile_and_emf_format():
    from cloud.metrics import Metrics, emf_line, percentile
    assert percentile([1, 2, 3, 4, 100], 50) == 3 and percentile([], 95) is None and percentile([5], 95) == 5
    line = json.loads(emf_line("Nimbus", {"Service": "s"}, {"QueueDepth": (3, "Count")}, 1700000000000))
    assert line["QueueDepth"] == 3 and line["_aws"]["CloudWatchMetrics"][0]["Metrics"] == [{"Name": "QueueDepth", "Unit": "Count"}]
    m = Metrics(); [m.observe("x", v) for v in range(1, 101)]
    assert m.snapshot()["latency"]["x"]["p95"] == 95.05


def test_emf_is_printed_only_when_switched_on(api, monkeypatch, capsys):
    import server
    from cloud_helpers import make_orch
    orch = make_orch()
    server.emit_metrics(orch, [])
    assert capsys.readouterr().out == ""
    monkeypatch.setenv("NIMBUS_EMF", "1"); server._EMF_STATE["last"] = 0
    server.emit_metrics(orch, [])
    out = capsys.readouterr().out.strip().splitlines()[-1]
    assert json.loads(out)["_aws"]["CloudWatchMetrics"][0]["Namespace"] == "Nimbus"
