"""The /map page and its JSON: coordinates from P1's map, closures, restrictions, robots, routes, reopening."""

from urllib import request as urlreq

from conftest import http_json


def sim(monkeypatch):
    monkeypatch.setenv("NIMBUS_PACK", "hospital_sim")


def test_map_page_is_served(api):
    with urlreq.urlopen(api + "/map") as r:
        html = r.read().decode()
    assert r.status == 200 and "Nimbus live map" in html and "textContent" in html


def test_map_json_has_p1_coordinates_edges_and_fleet(api, monkeypatch):
    sim(monkeypatch)
    import server, threading, tempfile, pathlib
    httpd = server.make_server("127.0.0.1", 0, pathlib.Path(tempfile.mkdtemp()))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        _, m = http_json("GET", base + "/map.json")
        nodes = {n["id"]: n for n in m["nodes"]}
        assert (nodes["east_waiting_approach"]["x"], nodes["east_waiting_approach"]["y"]) == (4.5, 8.0)   # P1's waypoint
        assert len(m["edges"]) == 17 and {r["id"] for r in m["robots"]} == {"robot_1", "robot_2"}
    finally:
        httpd.shutdown()


def test_hand_made_map_gets_an_automatic_layout(api):
    _, m = http_json("GET", api + "/map.json")
    assert m["pack"].startswith("hospital@") and all(isinstance(n["x"], float) for n in m["nodes"])
    assert len({(n["x"], n["y"]) for n in m["nodes"]}) == len(m["nodes"])      # no two nodes on top of each other


def test_lease_closure_and_restriction_show_up_and_reopen_clears_the_closure(api):
    _, b = http_json("POST", api + "/requests", {"text": "Deliver a wheelchair to 2B-3, routine.", "message_id": "mv1"})
    http_json("POST", api + "/requests", {"text": "Ward 2C is now isolation.", "message_id": "mv2"})
    http_json("POST", api + "/escalations", {"type": "UNEXPECTED_EVENT", "message_id": "mv3", "lease_version": 1, "task_id": b["task_id"],
                                             "payload": {"what_happened": "E3 blocked", "where": "E3", "robots_saw": ["cart"],
                                                         "segments": [{"segment_id": "E3", "state": "blocked"}]}})
    _, m = http_json("GET", api + "/map.json")
    lease = next(l for l in m["leases"] if l["task_id"] == b["task_id"])
    assert lease["version"] == 2 and "E3" not in lease["route"] and lease["route"][0] == "E-STORE" and lease["route"][-1] == "2B-3" or lease["route"][-1] == "2B"
    assert "E3" in m["closed"] and "2C" in m["restrictions"]
    assert any(t["task_id"] == b["task_id"] and t["live"] for t in m["tasks"]) and m["notices"]
    assert http_json("POST", api + "/map/reopen", {"zone": "E3"})[0] == 201
    assert "E3" not in http_json("GET", api + "/map.json")[1]["closed"]
    assert http_json("POST", api + "/map/reopen", {"zone": "nowhere"})[0] == 400


def test_page_has_the_controls_a_person_needs(api):
    with urlreq.urlopen(api + "/map") as r:
        html = r.read().decode()
    for needle in ("Needs your answer", "Replay", "Re-run this input", "possible_readings", "/tasks/${selected}/trace", "setInterval(frame"):
        assert needle in html, needle
    assert "innerHTML" not in html          # request text reaches the page: it must only ever be inserted as text


def test_map_json_carries_the_question_so_the_page_can_answer_it(api):
    _, b = http_json("POST", api + "/requests", {"text": "Need an infusion pump at 2B-12, urgent.", "message_id": "q1"})
    m = http_json("GET", api + "/map.json")[1]
    t = next(x for x in m["tasks"] if x["task_id"] == b["task_id"])
    assert t["status"] == "needs_clarification" and t["question"]["possible_readings"]
    st, _ = http_json("POST", api + f"/requests/{b['task_id']}/answer", {"text": t["question"]["possible_readings"][0], "message_id": "q1a"})
    assert st == 201 and http_json("GET", api + f"/tasks/{b['task_id']}")[1]["status"] == "leased"


def test_robot_position_digest_is_validated_and_shown(api):
    http_json("POST", api + "/fleet/heartbeat", {"cluster_id": "east-wing", "robots": [{"id": "R1", "state": "busy", "zone": "E2", "pos": [1.5, 2.5]}]})
    http_json("POST", api + "/fleet/heartbeat", {"cluster_id": "west-wing", "robots": [{"id": "R4", "state": "busy", "zone": "W2", "pos": "bad"}]})
    robots = {r["id"]: r for r in http_json("GET", api + "/map.json")[1]["robots"]}
    assert robots["R1"]["pos"] == [1.5, 2.5] and robots["R4"]["pos"] is None
