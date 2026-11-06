#!/usr/bin/env python3
"""Run the cloud-side (Person 3 / shared) claims of PRD v6 Table 4 and write evidence to P3_Results/.

    python scripts/run_p3_claims.py --out P3_Results

Everything runs offline on the model stand-in (rule-based): it measures the system around the model (scheduling, rule checker, fallback,
leases, replay, fan-out), NOT the quality of a real Bedrock model. Where a claim needs Bedrock the README says so and gives the command.

Writes: e5_bedrock_down.csv (FR-18), e6_sharing.csv (FR-11, FR-17 map part), e6_replay.json (FR-20), nfr1_request_to_lease.csv (NFR-1 cloud half),
fr_checks.csv (FR-14, FR-15, FR-16, FR-22), claims_summary.json (all numbers the README quotes).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib import request as urlreq
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("NIMBUS_PACK", "hospital_sim6")

import contracts  # noqa: E402

PORT = 8300
_topo = json.loads((ROOT / "simulation" / "hospital_world" / "topology" / "hospital_topology.json").read_text())
WINGS = {n: d.get("wing", "shared") for n, d in _topo["nodes"].items()}     # P1's map: which wing each zone belongs to


# ----------------------------------------------------------------------------- tiny HTTP + server harness
def call(api, method, path, body=None):
    req = urlreq.Request(api + path, data=None if body is None else json.dumps(body).encode(), method=method,
                         headers={"Content-Type": "application/json"})
    try:
        with urlreq.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


class Server:
    def __init__(self, pack="hospital_sim6", port=PORT, env=None):
        self.port, self.pack = port, pack
        self.dir = Path(tempfile.mkdtemp(prefix="p3claims-"))
        e = dict(os.environ, NIMBUS_PACK=pack, PORT=str(port), DATA_DIR=str(self.dir), NIMBUS_TICK_S="0",
                 NIMBUS_DEMO_FAULTS="1", NIMBUS_BACKEND="local", NIMBUS_LLM="fake", NIMBUS_ALLOW_UNKNOWN="0")
        e.pop("NIMBUS_EDGE_TOKEN", None)
        e.update(env or {})
        self.p = subprocess.Popen([sys.executable, "-u", str(ROOT / "server.py")], env=e, cwd=str(ROOT),
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.api = f"http://127.0.0.1:{port}"
        for _ in range(60):
            try:
                if call(self.api, "GET", "/fleet")[0] == 200:
                    return
            except Exception:
                pass
            time.sleep(0.25)
        raise RuntimeError("server did not start")

    def stop(self):
        self.p.terminate()
        try:
            self.p.wait(5)
        except Exception:
            self.p.kill()
        shutil.rmtree(self.dir, ignore_errors=True)


_n = [0]


def mid(prefix="m"):
    _n[0] += 1
    return f"{prefix}-{os.getpid()}-{_n[0]}"


def beat(api):
    """What the two edge supervisors do every 15 s: report each cluster alive with its robots (health 'unknown' is not leasable)."""
    _, f = call(api, "GET", "/fleet")
    for c in f["fleet"]:
        call(api, "POST", "/fleet/heartbeat", {"cluster_id": c["cluster_id"], "robots": [{"id": r["id"], "state": "idle", "zone": r["zone"], "battery": 1.0} for r in c["robots"]]})


def lease_task(api, text):
    """POST a request, answer one clarification if asked, return (task_id, task_dict)."""
    beat(api)
    t0 = time.perf_counter()
    _, b = call(api, "POST", "/requests", {"text": text, "message_id": mid("req")})
    if b.get("status") == "needs_clarification":
        reading = (b.get("question") or {}).get("possible_readings", ["yes"])[0]
        _, b = call(api, "POST", f"/requests/{b['task_id']}/answer", {"text": reading, "message_id": mid("ans")})
    return b.get("task_id"), b, time.perf_counter() - t0


def finish(api, task):
    lease = task.get("lease") or {}
    if lease:
        call(api, "POST", "/escalations", {"type": "OUTPUT_READY", "message_id": mid("out"), "lease_version": lease["version"],
                                           "task_id": task["task_id"], "payload": {"result_ref": f"storage://results/{task['task_id']}/x.jpg"}})


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))] if xs else None


# ----------------------------------------------------------------------------- E5 / FR-18: model unavailable for 10 minutes
def e5_bedrock_down(out: Path, summary: dict):
    from cloud.clock import FixedClock
    from cloud.faults import FaultyLLM
    from cloud.llm import CircuitBreakerLLM, RuleBasedLLM
    from cloud.pack import Pack
    from cloud.trace import TracingLLM
    from cloud_helpers import make_orch, new_task

    clock = FixedClock()
    pack = Pack.load("hospital_sim6")
    orch = make_orch(clock=clock, pack=pack)
    faulty = FaultyLLM(RuleBasedLLM(orch.pack, orch.topo), clock)
    orch.llm = TracingLLM(CircuitBreakerLLM(faulty, clock), orch.trace, orch.metrics)
    asks = [("blanket", "ward 2B"), ("blanket", "ward 2C"), ("iv pole", "ward 2C"), ("iv pole", "ward 2B"), ("blanket", "ward 3")]
    rows, n, live = [], [0], []

    def request(phase):
        n[0] += 1
        item, ward = asks[n[0] % len(asks)]
        text = f"Please bring a {item} to {ward}, routine."
        t = new_task(n[0], text)
        out_ = orch.handle_request(t, text)
        t.update(spec=out_.spec, lease=out_.lease, status=out_.kind, reservation=out_.reservation, request_text=text)
        row = out_.rows[0]["output"] if out_.rows else {}
        proposed_by = row.get("proposed_by", "")
        llm_calls = (row.get("llm") or {}).get("calls", 0)
        valid = bool(out_.lease) and contracts.validate_document("lease", out_.lease) == []
        rows.append({"t_s": int(clock.now().timestamp() - t0), "phase": phase, "request": text, "outcome": out_.kind,
                     "lease_valid": int(valid), "proposed_by": proposed_by, "model_calls_for_this_request": llm_calls,
                     "user_told_reduced_autonomy": int(bool(out_.notice and "Reduced autonomy" in out_.notice))})
        if out_.lease:
            live.append(t)
        if len(live) > 1:                       # keep robots free: the oldest leased task is delivered
            orch.finish(live.pop(0))
        return t, out_

    t0 = clock.now().timestamp()
    for _ in range(4):
        request("before outage"); clock.advance(20)
    held, _ = request("before outage")          # this lease is live when the outage starts
    faulty.fail_for(600)
    renew_ok = []
    for k in range(10):                         # ten minutes of outage, one request a minute
        clock.advance(55)
        request("during outage (model down)")
        try:                                    # a lease issued before the outage keeps being renewed (no model involved)
            r = orch.renew(held["task_id"], held["lease"]["version"])
            renew_ok.append(True)
        except Exception:
            renew_ok.append(False)
    clock.advance(10)
    for _ in range(3):
        request("after outage (breaker cool-down)"); clock.advance(20)
    clock.advance(40)
    for _ in range(4):
        request("model back"); clock.advance(20)
    with open(out / "e5_bedrock_down.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    during = [r for r in rows if r["phase"].startswith("during")]
    summary["e5_bedrock_down"] = {
        "requests_during_outage": len(during),
        "leased_during_outage": sum(r["outcome"] == "leased" for r in during),
        "model_calls_made_during_outage_(breaker_skips_it)": sum(r["model_calls_for_this_request"] for r in during),
        "leases_issued_by_model_path_during_outage": sum(r["outcome"] == "leased" and r["model_calls_for_this_request"] > 0 for r in during),
        "valid_leases_during_outage": sum(r["lease_valid"] for r in during),
        "user_told_reduced_autonomy": sum(r["user_told_reduced_autonomy"] for r in during),
        "leased_before_outage_still_renewed_during_outage": all(renew_ok),
        "model_used_again_after_recovery": any(r["phase"] == "model back" and r["model_calls_for_this_request"] > 0 and r["outcome"] == "leased" and not r["user_told_reduced_autonomy"] for r in rows),
        "model_failures_seen": faulty.failed_calls,
    }


# ----------------------------------------------------------------------------- E6 / FR-11 + FR-17 map: selective sharing and map reflection
def e6_sharing(out: Path, summary: dict):
    zones = ["east_mid_hall", "east_south_hall", "east_north_gate", "east_waiting_approach",
             "west_mid_hall", "west_south_hall", "west_north_gate", "west_waiting_approach",
             "nurse_station_approach", "north_hall", "south_equipment_approach"]
    rows = []
    for zone in zones:
        s = Server()
        try:
            api = s.api
            east_id, east, _ = lease_task(api, "Please bring a wheelchair to ward 2B")
            west_id, west, _ = lease_task(api, "Please bring a wheelchair to ward 2C")
            _, tasks = call(api, "GET", "/tasks")
            tl = {t["task_id"]: t for t in tasks["tasks"]}
            # ground truth from P1's map and the live leases (not from the code under test): a cluster is affected when the zone lies in its
            # wing, is on the shared spine, or is inside one of its live leases
            _, fl = call(api, "GET", "/fleet")
            area = {c["cluster_id"]: c["area"] for c in fl["fleet"]}
            wing = WINGS.get(zone, "shared")
            expected = sorted({t["lease"]["cluster_id"] for t in tl.values() if t.get("lease") and zone in t["lease"]["allowed_zones"]}
                              | {c for c, a in area.items() if wing in ("shared", a)})
            _, outbox0 = call(api, "GET", "/outbox")
            n0 = len(outbox0["outbox"])
            victim = east_id
            lv = tl[victim]["lease"]["version"]
            t_post = time.perf_counter()
            call(api, "POST", "/escalations", {"type": "UNEXPECTED_EVENT", "message_id": mid("ue"), "lease_version": lv, "task_id": victim,
                                               "payload": {"what_happened": f"{zone} blocked", "where": zone, "robots_saw": ["robot saw a blockage"],
                                                           "segments": [{"segment_id": zone, "state": "blocked"}]}})
            reflect = None
            for _ in range(100):
                _, mp = call(api, "GET", "/map.json")
                closed = [z for z in (mp.get("closed") or mp.get("closures") or [])]
                names = {(c if isinstance(c, str) else c.get("zone") or c.get("id")) for c in closed}
                if zone in names or any(zone in json.dumps(c) for c in closed):
                    reflect = time.perf_counter() - t_post
                    break
                time.sleep(0.05)
            _, outbox1 = call(api, "GET", "/outbox")
            new = outbox1["outbox"][n0:]
            notified = sorted({i["topic"].split("/")[1] for i in new if i["topic"].endswith("/notice") and i["payload"].get("type") == "MAP"
                               and zone in i["payload"].get("text", "")})
            unaffected = sorted(set(notified) - set(expected))
            rows.append({"zone_blocked": zone, "affected_clusters(ground truth)": "+".join(expected) or "-",
                         "clusters_informed": "+".join(notified) or "-", "all_affected_informed": int(set(expected) <= set(notified)),
                         "unaffected_clusters_informed": len(unaffected), "seconds_until_map_shows_it": None if reflect is None else round(reflect, 3)})
        finally:
            s.stop()
    with open(out / "e6_sharing.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    relevant = [r for r in rows if r["affected_clusters(ground truth)"] != "-"]
    summary["e6_sharing"] = {
        "trials": len(rows), "trials_with_an_affected_cluster": len(relevant),
        "affected_clusters_all_informed": sum(r["all_affected_informed"] for r in relevant),
        "unaffected_clusters_informed_total": sum(r["unaffected_clusters_informed"] for r in rows),
        "map_reflected_within_5_min": sum(r["seconds_until_map_shows_it"] is not None for r in rows),
        "max_seconds_until_map_shows_it": max([r["seconds_until_map_shows_it"] or 0 for r in rows]),
    }


# ----------------------------------------------------------------------------- E6 / FR-20: replay a task's timeline from the decision log
def fold(rows):
    """Rebuild a task's state ONLY from its decision-log rows."""
    st = {"status": "received", "lease_version": None, "cluster": None}
    for r in rows:
        o = r["output"] or {}
        d, e = o.get("decision"), o.get("event")
        if d == "ask_clarify":
            st["status"] = "needs_clarification"
        elif d == "issue_lease":
            st.update(status="leased", lease_version=r["lease_version"], cluster=o.get("cluster") or st["cluster"])
        elif d == "propose_lease":
            st["cluster"] = o.get("cluster") or st["cluster"]
        elif e == "OUTPUT_READY":
            st["status"] = "completed"
        elif d == "operator_cancel":
            st["status"] = "cancelled"
        elif d == "operator_pause":
            st["status"] = "paused"
        elif d == "stand_down":
            pass
        elif d == "queue":
            st["status"] = "queued"
    return st


def e6_replay(out: Path, summary: dict):
    s = Server()
    res = []
    try:
        api = s.api
        a, ta, _ = lease_task(api, "Need an infusion pump at 2B-12, urgent")           # SC-1 incl. a clarification
        finish(api, ta | {"task_id": a, "lease": ta.get("lease")})
        b, tb, _ = lease_task(api, "Please bring a wheelchair to ward 2B")              # SC-2: re-plan then complete
        call(api, "POST", f"/tasks/{b}/replan", {"blocked_zones": ["east_waiting_approach"], "reason": "scripted", "message_id": mid("rp")})
        c, tc, _ = lease_task(api, "Please bring a blanket to ward 2C")                 # cancelled by the operator
        call(api, "POST", f"/tasks/{c}/control", {"action": "cancel", "message_id": mid("c")})
        for tid in (a, b, c):
            _, tl = call(api, "GET", f"/tasks/{tid}/timeline")
            _, task = call(api, "GET", f"/tasks/{tid}")
            rows = tl["timeline"]
            replayed = fold(rows)
            ts = [r["timestamp"] for r in rows]
            version = (task.get("lease") or {}).get("version")
            res.append({"task_id": tid, "request": task["request_text"], "rows": len(rows),
                        "who_decided": sorted({r["who"] for r in rows}),
                        "timestamps_in_order": ts == sorted(ts),
                        "replayed_status": replayed["status"], "actual_status": task["status"],
                        "replayed_lease_version": replayed["lease_version"], "actual_lease_version": version,
                        "status_matches": replayed["status"] == task["status"],
                        "every_row_has_who_inputs_output_version": all(("who" in r and "inputs" in r and "output" in r and "lease_version" in r) for r in rows)})
    finally:
        s.stop()
    (out / "e6_replay.json").write_text(json.dumps({"method": "state rebuilt only from GET /tasks/{id}/timeline rows, compared with GET /tasks/{id}",
                                                    "tasks": res}, indent=1))
    summary["e6_replay"] = {"tasks": len(res), "status_matches": sum(r["status_matches"] for r in res),
                            "all_rows_have_who_inputs_output_version": all(r["every_row_has_who_inputs_output_version"] for r in res)}


# ----------------------------------------------------------------------------- NFR-1 (cloud half) request -> lease
def nfr1(out: Path, summary: dict):
    texts = []
    for item in ("wheelchair", "blanket", "monitor", "iv pole", "infusion pump"):
        for ward in ("ward 2B", "ward 2C", "ward 3A", "ward 3"):
            texts.append(f"Please bring a {item} to {ward}")
    texts = (texts * 3)[:50]
    s = Server()
    rows = []
    try:
        for i, t in enumerate(texts):
            tid, b, wall = lease_task(s.api, t)
            _, task = call(s.api, "GET", f"/tasks/{tid}")
            rows.append({"n": i + 1, "request": t, "status": task["status"], "wall_seconds_request_to_lease": round(wall, 4)})
            finish(s.api, task)
        _, m = call(s.api, "GET", "/metrics")
    finally:
        s.stop()
    with open(out / "nfr1_request_to_lease.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    leased = [r["wall_seconds_request_to_lease"] for r in rows if r["status"] == "leased"]
    summary["nfr1_cloud_half"] = {"requests": len(rows), "leased": len(leased), "p50_s": round(pct(leased, 50), 4), "p95_s": round(pct(leased, 95), 4),
                                  "server_metric_request_to_lease_s": (m.get("latency") or {}).get("request_to_lease_s")}


# ----------------------------------------------------------------------------- FR-14 / FR-15 / FR-16 / FR-22 checks
def fr_checks(out: Path, summary: dict):
    from cloud.clock import FixedClock
    from cloud.llm import LLMError
    from cloud.pack import Pack
    from cloud_helpers import make_orch, new_task, tool
    rows = []

    # FR-15: no invalid lease; a proposal the checker rejects twice -> simple rule-based assignment
    pack = Pack.load("hospital_sim6")
    invalid = total = 0
    asks = [("wheelchair", w) for w in ("ward 2B", "ward 2C", "ward 3A")] + [("blanket", w) for w in ("ward 2B", "ward 2C")] + [("monitor", "ward 2B")]
    for k in range(60):
        orch = make_orch(clock=FixedClock(), pack=pack)
        item, ward = asks[k % len(asks)]
        text = f"Please bring a {item} to {ward}" + (", urgent" if k % 3 == 0 else "")
        t = new_task(k + 1, text)
        o = orch.handle_request(t, text)
        if o.kind == "leased":
            total += 1
            invalid += contracts.validate_document("lease", o.lease) != []
    rows.append({"requirement": "FR-15", "check": "of the leases issued for 60 requests (urgent and routine, 3 wards), how many fail the lease schema", "result": f"{invalid} of {total}", "pass": int(invalid == 0)})

    bad_calls = []

    def bad_proposal(tools, kw):          # the model proposes a robot that does not exist, every time it is asked
        if "propose_allocation" in tools:
            bad_calls.append(1)
            return tool("propose_allocation", cluster_id="east-wing", robot_ids=["robot_9"], rationale="trust me")
        return None
    sim = Pack.load("hospital_sim")       # ward 3 is shared there: two clusters can take it, so the model is really asked
    orch = make_orch(clock=FixedClock(), pack=sim, override=bad_proposal)
    t = new_task(1, "Deliver a wheelchair to ward 3, routine.")
    o = orch.handle_request(t, "Deliver a wheelchair to ward 3, routine.")
    att = (o.rows[0]["output"] or {}).get("attempts", []) if o.rows else []
    proposed_by = (o.rows[0]["output"] or {}).get("proposed_by") if o.rows else None
    vetoed = sum(1 for x in att if x.get("source") == "llm" and not x.get("ok"))
    rows.append({"requirement": "FR-15", "check": "model proposes a non-existent robot every time: checker vetoes, lease is then issued by the deterministic assigner after 2 failed proposals",
                 "result": f"outcome={o.kind}, model proposals vetoed={vetoed}, lease proposed_by={proposed_by}, lease valid={contracts.validate_document('lease', o.lease) == []}",
                 "pass": int(o.kind == "leased" and proposed_by == "classical" and vetoed == 2 and contracts.validate_document("lease", o.lease) == [])})

    # FR-14: urgent never waits behind routine (one free robot)
    served_first = 0
    N = 20
    for k in range(N):
        from cloud.fleet import FleetRegistry
        orch = make_orch(clock=FixedClock(), pack=pack)
        # occupy every robot of the east cluster with routine deliveries, queue a routine then an urgent request
        busy = []
        for j in range(8):
            t = new_task(100 + j, "Please bring a wheelchair to ward 2B")
            o = orch.handle_request(t, "Please bring a wheelchair to ward 2B")
            t.update(spec=o.spec, lease=o.lease, status=o.kind, reservation=o.reservation, request_text="x")
            busy.append(t)
        leased_busy = [t for t in busy if t["status"] == "leased"]
        queued = []
        for j, (txt, pr) in enumerate((("Please bring a wheelchair to ward 2B", "routine"), ("Need a wheelchair at ward 2B, urgent", "urgent"))):
            t = new_task(200 + j, txt)
            o = orch.handle_request(t, txt)
            t.update(spec=o.spec, lease=o.lease, status=o.kind, reservation=o.reservation, request_text=txt)
            queued.append((t, pr))
        if not leased_busy:
            continue
        orch.finish(leased_busy[0])                       # one robot frees up
        out_list = list(orch.drain(lambda tid: next((q[0] for q in queued if q[0]["task_id"] == tid), None)))
        first = out_list[0][0]["task_id"] if out_list else None
        urgent_id = queued[1][0]["task_id"]
        served_first += int(first == urgent_id)
    rows.append({"requirement": "FR-14", "check": f"{N} trials: urgent request queued AFTER a routine one gets the freed robot first", "result": f"{served_first} of {N}", "pass": int(served_first == N)})

    # FR-16 + FR-22 timing over HTTP (cloud-side compute; WAN/edge not included). The hand-made `hospital` pack is used for the re-plan
    # trials because its map has an alternative route around E3 (P1's six-robot map has a single corridor to each ward, so a closure there
    # correctly ends in an operator alert rather than a new lease).
    replan_t, cancel_t = [], []
    for k in range(12):                    # a fresh cloud per trial: no leftover robots or stock from earlier trials
        s = Server(pack="hospital")
        try:
            tid, b, _ = lease_task(s.api, "Need an infusion pump at 2B-12, urgent")
            _, task = call(s.api, "GET", f"/tasks/{tid}")
            if task.get("lease"):
                t0 = time.perf_counter()
                st, r = call(s.api, "POST", f"/tasks/{tid}/replan", {"blocked_zones": ["E3"], "reason": "scripted", "message_id": mid("rp")})
                ok = st == 201 and (r.get("lease") or {}).get("version") == 2 and contracts.validate_document("lease", r["lease"]) == []
                replan_t.append((time.perf_counter() - t0, ok))
            tid2, b2, _ = lease_task(s.api, "Deliver a blanket to 2A, routine.")
            _, t2 = call(s.api, "GET", f"/tasks/{tid2}")
            if t2.get("lease"):
                _, ob0 = call(s.api, "GET", "/outbox")
                n0 = len(ob0["outbox"])
                t0 = time.perf_counter()
                call(s.api, "POST", f"/tasks/{tid2}/control", {"action": "cancel", "message_id": mid("c")})
                _, ob1 = call(s.api, "GET", "/outbox")
                stop_seen = any(i["topic"].endswith("/lease") and len(i["payload"]["task_sequence"]) == 1 and i["payload"]["task_sequence"][0]["cmd"] == "stop"
                                for i in ob1["outbox"][n0:])
                cancel_t.append((time.perf_counter() - t0, stop_seen))
        finally:
            s.stop()
    rp = [t for t, ok in replan_t]
    good = sum(ok for _, ok in replan_t)
    rows.append({"requirement": "FR-16", "check": "blocked zone -> re-plan -> valid lease v2: cloud processing time (stand-in model; excludes model and WAN latency)",
                 "result": f"{good} of {len(replan_t)} re-plans gave a valid v2 lease; p95 {pct(rp, 95):.3f}s, max {max(rp):.3f}s",
                 "pass": int(good == len(replan_t) and len(replan_t) >= 10 and pct(rp, 95) < 30)})
    ct = [t for t, ok in cancel_t]
    rows.append({"requirement": "FR-22", "check": "operator cancel -> stop lease visible to the edge in the outbox (12 trials, cloud side only)",
                 "result": f"p95 {pct(ct, 95):.3f}s; stop lease published in {sum(ok for _, ok in cancel_t)} of {len(cancel_t)}", "pass": int(all(ok for _, ok in cancel_t) and pct(ct, 95) < 5)})
    with open(out / "fr_checks.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["requirement", "check", "result", "pass"]); w.writeheader(); w.writerows(rows)
    summary["fr_checks"] = rows


# ----------------------------------------------------------------------------- E3 log excerpts: REAL decision-log rows from fresh runs
def e3_excerpts(out: Path, summary: dict):
    from cloud.clock import FixedClock
    from cloud.pack import Pack
    from cloud_helpers import make_orch, new_task, tool
    (out / "e3").mkdir(exist_ok=True)
    md = ["# E3 log excerpts (generated by scripts/run_p3_claims.py from real runs; nothing here is typed by hand)", ""]

    def block(title, rows):
        md.extend([f"## {title}", "", "```json", json.dumps(rows, indent=1, default=str), "```", ""])

    # 1. the rule checker approves before a lease is issued (SC-1 on the hospital map)
    s = Server(pack="hospital")
    try:
        tid, b, _ = lease_task(s.api, "Need an infusion pump at 2B-12, urgent")
        _, tl = call(s.api, "GET", f"/tasks/{tid}/timeline")
        keep = [r for r in tl["timeline"] if (r["output"] or {}).get("decision") in ("propose_lease", "approve", "issue_lease", "ask_clarify")]
        block("1. Rule checker approves, then the lease is issued (SC-1, real GET /tasks/{id}/timeline rows)",
              [{"who": r["who"], "decision": r["output"].get("decision"), "lease_version": r["lease_version"], "timestamp": r["timestamp"],
                **({"violations": r["output"].get("violations")} if "violations" in r["output"] else {}),
                **({"cluster": r["output"].get("cluster"), "robots": r["output"].get("robots"), "proposed_by": r["output"].get("proposed_by"),
                    "attempts": r["output"].get("attempts")} if r["output"].get("decision") == "propose_lease" else {})} for r in keep])
        # 4. re-plan
        task = call(s.api, "GET", f"/tasks/{tid}")[1]
        t0 = time.perf_counter()
        st, rp = call(s.api, "POST", f"/tasks/{tid}/replan", {"blocked_zones": ["E3"], "reason": "scripted", "message_id": mid("rp")})
        dt = time.perf_counter() - t0
        _, tl2 = call(s.api, "GET", f"/tasks/{tid}/timeline")
        after = [r for r in tl2["timeline"] if (r["output"] or {}).get("decision") in ("replan", "propose_lease", "approve", "issue_lease") and r["lease_version"] in (2, None)][-4:]
        block(f"4. Re-plan: blocked zone E3 -> lease v2 (cloud processing {dt*1000:.1f} ms, HTTP {st}; stand-in model, no WAN)",
              [{"who": r["who"], "decision": r["output"].get("decision"), "lease_version": r["lease_version"], "timestamp": r["timestamp"]} for r in after])
    finally:
        s.stop()
    # 2. fallback after two failed proposals
    def bad(tools, kw):
        if "propose_allocation" in tools:
            return tool("propose_allocation", cluster_id="east-wing", robot_ids=["robot_9"], rationale="trust me")
    orch = make_orch(clock=FixedClock(), pack=Pack.load("hospital_sim"), override=bad)
    o = orch.handle_request(new_task(1, "x"), "Deliver a wheelchair to ward 3, routine.")
    row = o.rows[0]
    block("2. Two vetoed model proposals, then the deterministic assigner (FR-15)",
          {"who": row["who"], "decision": row["output"].get("decision"), "proposed_by": row["output"].get("proposed_by"),
           "attempts": row["output"].get("attempts"), "rationale": row["output"].get("rationale")})
    # 3. urgent before routine
    orch = make_orch(clock=FixedClock(), pack=Pack.load("hospital_sim6"))
    leased, queued = [], []
    for j in range(8):
        t = new_task(100 + j, "Please bring a wheelchair to ward 2B")
        oo = orch.handle_request(t, "Please bring a wheelchair to ward 2B")
        t.update(spec=oo.spec, lease=oo.lease, status=oo.kind, reservation=oo.reservation, request_text="x")
        (leased if oo.kind == "leased" else queued).append(t)
    tasks = {}
    for j, (txt, pr) in enumerate((("Please bring a wheelchair to ward 2B", "routine"), ("Need a wheelchair at ward 2B, urgent", "urgent"))):
        t = new_task(200 + j, txt)
        oo = orch.handle_request(t, txt)
        t.update(spec=oo.spec, lease=oo.lease, status=oo.kind, reservation=oo.reservation, request_text=txt)
        tasks[t["task_id"]] = (t, pr)
    orch.finish(leased[0])
    served = [(t["task_id"], tasks[t["task_id"]][1] if t["task_id"] in tasks else "?") for t, _ in orch.drain(lambda tid: tasks.get(tid, (None,))[0])]
    block("3. A robot frees up with a routine (queued first) and an urgent (queued second) request waiting (FR-14)",
          {"order_requests_were_queued": [(k, v[1]) for k, v in tasks.items()], "request_served_first": served[:1]})
    (out / "e3" / "log_excerpts.md").write_text("\n".join(md))
    summary["e3_excerpts"] = {"file": "e3/log_excerpts.md", "urgent_served_first": bool(served) and served[0][1] == "urgent"}


# ----------------------------------------------------------------------------- the existing experiment tools, run into P3_Results
def _run(cmd, capture=None, timeout=900, env=None):
    r = subprocess.run([sys.executable] + cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout, env=dict(os.environ, **(env or {})))
    if capture:
        Path(capture).write_text(r.stdout + r.stderr + f"\nexit code: {r.returncode}\n")
    return r


def e2(out: Path, summary: dict):
    rows = []
    for cfg in ("C1", "C3"):
        r = _run(["scripts/score_e2.py", "--config", cfg, "--set", "all"])
        d = json.loads(r.stdout[r.stdout.index("{"):])
        q = d["requests"]
        base = dict(config=cfg, model=d["model"], labels="DRAFT (template-written, not yet reviewed by a person)")
        rows += [
            dict(base, requirement="FR-13", metric="field accuracy: item, destination, priority, deadline over 50 requests", value=q["field_accuracy"], target=">= 0.85", n=q["n"]),
            dict(base, requirement="FR-13", metric="clarification recall: vague requests that got a question", value=q["clarification_recall"], target="most vague ones asked", n=q["n"]),
            dict(base, requirement="FR-13", metric="clarification precision", value=q["clarification_precision"], target="-", n=q["n"]),
            dict(base, requirement="FR-17", metric="plain-language rules applied correctly", value=f"{d['rules']['applied_correctly']}/{d['rules']['n']}", target="10 of 10", n=d["rules"]["n"]),
            dict(base, requirement="FR-19", metric="context-dependent requests resolved correctly (habit/control/override)", value=f"{d['context']['correct']}/{d['context']['n']}", target="most resolved without asking", n=d["context"]["n"]),
        ]
    with open(out / "e2_cloud.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    summary["e2"] = {"rows": len(rows)}


def e3(out: Path, summary: dict):
    """T8 ("three requests, two robots") is written for a two-robot cluster. `hospital_sim` has two robots per cluster (the design
    the experiment was written for); the six-robot `hospital_sim6` has three, so there all three requests are leased at once and T8's
    fixture check (2 leased + 1 queued) cannot hold. Both are run and reported."""
    for pack, sub in (("hospital_sim", "e3"), ("hospital_sim6", "e3_sim6")):
        (out / sub).mkdir(exist_ok=True)
        _run(["scripts/run_e3.py", "--seeds", "10", "--out", str(out / sub)], capture=out / sub / "e3_run.txt", env={"NIMBUS_PACK": pack})
        _run(["scripts/e3_report.py", "--in", str(out / sub / "results.json"), "--out", str(out / sub / "e3-results.md")])


def e7(out: Path, summary: dict):
    (out / "e7").mkdir(exist_ok=True)
    _run(["scripts/run_e7.py", "--robots", "3,6,10,20", "--hours", "24", "--seed", "1", "--out", str(out / "e7")], capture=out / "e7" / "e7_run.txt", timeout=1800)


def seeds(out: Path, summary: dict):
    text = []
    for sd in (7, 11, 23):
        r = _run(["scripts/replay_check.py", "--seed", str(sd)])
        text.append(f"seed {sd}: exit {r.returncode}\n{r.stdout}{r.stderr}")
    (out / "e6_seed_diff.txt").write_text("\n".join(text))


def audits(out: Path, summary: dict):
    _run(["scripts/audit_data.py"], capture=out / "nfr5_audit_data.txt")
    _run(["scripts/audit_iam.py"], capture=out / "nfr5_audit_iam.txt")


def inject(out: Path, summary: dict):
    import threading
    s = Server(pack="hospital")             # the hand-made map has an alternative route, so a re-plan to lease v2 is possible
    stop = []

    def hb():
        while not stop:
            try:
                beat(s.api)
            except Exception:
                pass
            time.sleep(1)
    threading.Thread(target=hb, daemon=True).start()
    time.sleep(1.5)
    try:
        r = _run(["scripts/inject_messages.py", "--api", s.api])
        (out / "e6_injection.txt").write_text("Local cloud, pack `hospital` (has an alternative route so a re-plan to v2 is possible); a heartbeat thread plays the edges.\n\n"
                                              + r.stdout + r.stderr + f"\nexit code: {r.returncode}\n")
        summary["e6_injection"] = {"exit_code": r.returncode}
    finally:
        stop.append(1)
        s.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "P3_Results"))
    ap.add_argument("--only", help="comma list: e2,e3,e7,seeds,audits,inject,e5,e6s,e6r,nfr1,fr,e3x")
    a = ap.parse_args()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    only = set((a.only or "e2,e3,e7,seeds,audits,inject,e5,e6s,e6r,nfr1,fr,e3x").split(","))
    summary = {}
    for key, fn in (("e2", e2), ("e3", e3), ("e7", e7), ("seeds", seeds), ("audits", audits), ("inject", inject), ("e5", e5_bedrock_down), ("e6s", e6_sharing), ("e6r", e6_replay), ("nfr1", nfr1), ("fr", fr_checks), ("e3x", e3_excerpts)):
        if key in only:
            t0 = time.time()
            print(f"== {key} ...", flush=True)
            fn(out, summary)
            print(f"   done in {time.time() - t0:.1f}s", flush=True)
    p = out / "claims_summary.json"
    old = json.loads(p.read_text()) if p.exists() else {}
    old.update(summary)
    p.write_text(json.dumps(old, indent=1, default=str))
    print(json.dumps(summary, indent=1, default=str))


if __name__ == "__main__":
    main()
