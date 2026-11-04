"""Watch MS2 run the way it really runs: the real server, the real HTTP API, a fake edge per cluster.

    python scripts/demo_ms2.py                    # SC-1, SC-2, SC-3 on P1's Gazebo map, narrated, then exit
    python scripts/demo_ms2.py --scenario sc2     # one scenario (sc1 | sc2 | sc3)
    python scripts/demo_ms2.py --pack hospital    # the hand-made v0 map instead
    python scripts/demo_ms2.py --serve            # after the story, keep the server up so you can open the browser
    python scripts/demo_ms2.py --mode C3          # no LLM at all (forms and rules)
    NIMBUS_LLM=bedrock NIMBUS_BEDROCK_MODEL=apac.amazon.nova-lite-v1:0 python scripts/demo_ms2.py   # real Bedrock

Each step prints what was sent, what the cloud decided, and the lease that went out. Nothing is mocked: it is
the same server.py, orchestrator, Rule Checker and lease store the deployed service runs. The offline model
stand-in answers unless you set NIMBUS_LLM=bedrock.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from urllib import request as urlreq  # noqa: E402
from urllib.error import HTTPError  # noqa: E402

BOLD, DIM, GREEN, YELLOW, RED, CYAN, OFF = "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[31m", "\033[36m", "\033[0m"
API = ""


def http(method: str, path: str, body: dict | None = None):
    data = None if body is None else json.dumps(body).encode()
    req = urlreq.Request(API + path, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        resp = urlreq.urlopen(req)
    except HTTPError as err:
        resp = err
    with resp:
        return resp.status, json.loads(resp.read().decode() or "{}")


def say(text: str = "") -> None:
    print(text)


def title(text: str) -> None:
    say(f"\n{BOLD}{CYAN}{'=' * 78}\n{text}\n{'=' * 78}{OFF}")


def step(text: str) -> None:
    say(f"\n{BOLD}> {text}{OFF}")


def show(label: str, value) -> None:
    say(f"  {DIM}{label:<14}{OFF} {value}")


def lease_line(lease: dict | None) -> str:
    if not lease:
        return f"{YELLOW}(no lease){OFF}"
    seq = " -> ".join(s["cmd"] + (f"({s['params'].get('to')})" if s["params"].get("to") else "") for s in lease["task_sequence"])
    return (f"{GREEN}v{lease['version']}{OFF} cluster={lease['cluster_id']} robot={','.join(lease['assigned_robots'])} "
            f"priority={lease['priority']}\n{'':17}steps: {seq}\n{'':17}allowed: {', '.join(lease['allowed_zones'])}"
            f"\n{'':17}forbidden: {', '.join(lease['forbidden_zones']) or '-'}   speed<= {lease['speed_limit']}")


def task(tid: str) -> dict:
    return http("GET", f"/tasks/{tid}")[1]


def timeline(tid: str, last: int = 12) -> None:
    rows = http("GET", f"/tasks/{tid}/timeline")[1]["timeline"]
    say(f"  {DIM}decision log (who decided what, FR-20):{OFF}")
    for r in rows[-last:]:
        out = r["output"]
        what = out.get("decision") or out.get("event") or ""
        extra = ""
        if out.get("action"):
            extra = f" action={out['action']}"
        if out.get("source"):
            extra += f" source={out['source']}"
        say(f"    v{r['lease_version'] or '-'} {r['who']:<9} {what}{extra}")


def notices(audience: str) -> None:
    items = http("GET", f"/notifications?audience={audience}")[1]["notifications"]
    say(f"  {DIM}notices to {audience}:{OFF}")
    for n in items[-4:]:
        say(f"    [{n['type']}] {n['text']}")
    if not items:
        say("    (none)")


# -- scenarios ---------------------------------------------------------------------------------------------------------

def sc1(EdgeSim) -> None:
    title("SC-1  A vague urgent request is clarified, then leased (PRD: 12-minute deadline, East wing)")
    step('Nurse: "Need an infusion pump at 2B-12, urgent."')
    _, b = http("POST", "/requests", {"text": "Need an infusion pump at 2B-12, urgent.", "message_id": "demo-sc1"})
    show("status", b["status"])
    show("cloud asks", f"{YELLOW}{b['question']['question']}{OFF}")
    tid = b["task_id"]
    step('Nurse answers: "Yes, a paediatric pump please."')
    http("POST", f"/requests/{tid}/answer", {"text": "Yes, a paediatric pump please.", "message_id": "demo-sc1-a"})
    t = task(tid)
    show("status", t["status"])
    show("spec", f"{t['spec']['item']} -> {t['spec']['destination']}, {t['spec']['priority']}, deadline {t['spec']['deadline_minutes']} min")
    show("lease", lease_line(t["lease"]))
    step("The east edge picks the lease up from the outbox, checks in, and finishes (OUTPUT_READY)")
    EdgeSim(API, t["lease"]["cluster_id"], complete=True).step()
    show("status", task(tid)["status"])
    timeline(tid)


def sc2(EdgeSim, pack: str) -> None:
    title("SC-2  Corridor blocked, then a gate closes: the cloud re-plans and re-assigns the last leg under a new version")
    sim = pack == "hospital_sim"
    text = "Move a monitor from 2B to 2C, routine"
    step(f'Request: "{text}"   (east wing to west wing: no single cluster covers both ends)')
    _, b = http("POST", "/requests", {"text": text, "message_id": "demo-sc2"})
    tid = b["task_id"]
    t = task(tid)
    show("status", t["status"])
    show("legs", " | ".join(f"leg{i + 1}: {l.get('source', '(store)')} -> {l['destination']}" for i, l in enumerate(t["legs"] or [])))
    show("lease", lease_line(t["lease"]))
    if not t["legs"]:
        say(f"\n  {RED}No legs: the request was read as one delivery inside a single wing (the source 'from 2B' was not "
            f"picked up), so there is no hand-over to show. Check the spec below.{OFF}")
        show("spec", t["spec"])
        return
    if not sim:
        say(f"\n  {YELLOW}Note: on the hand-made map one corridor block plus one closed gate cuts every route, so the cloud will "
            f"raise an operator alert instead of finding a way. Use --pack hospital_sim for the full story.{OFF}")
    block, park = ("east_mid_hall", "east_waiting_approach") if sim else ("E3", "E4")
    step(f"The east edge reports: corridor {block} is blocked (UNEXPECTED_EVENT), robot parked at {park}")
    EdgeSim(API, "east-wing", block=block, robot_zone=park).step()
    t = task(tid)
    show("status", t["status"])
    show("hand-off now", t["legs"][0]["destination"] if t["legs"] else "-")
    show("lease", lease_line(t["lease"]))
    step("Leg 1 finishes (OUTPUT_READY): the next lease version goes to the other cluster")
    http("POST", "/escalations", {"type": "OUTPUT_READY", "message_id": "demo-leg1", "lease_version": t["lease"]["version"],
                                  "task_id": tid, "payload": {"result_ref": "s3://nimbus-outputs/demo/leg1.jpg"}})
    t = task(tid)
    show("leg", f"{t['leg_index'] + 1} of {len(t['legs'])}")
    show("lease", lease_line(t["lease"]))
    gate, where = ("west_north_gate", "north_hall") if sim else ("W2", "W0")
    step(f"The west cluster reports: gate {gate} is closed (robot parked at {where})")
    http("POST", "/fleet/heartbeat", {"cluster_id": t["lease"]["cluster_id"],
                                      "robots": [{"id": t["lease"]["assigned_robots"][0], "state": "busy", "zone": where}]})
    _, body = http("POST", "/escalations", {
        "type": "UNEXPECTED_EVENT", "message_id": "demo-gate", "lease_version": t["lease"]["version"], "task_id": tid,
        "payload": {"what_happened": f"{gate} closed", "where": gate, "robots_saw": ["door shut"],
                    "segments": [{"segment_id": gate, "state": "closed"}]}})
    t = task(tid)
    show("replanned", body.get("replanned"))
    show("lease", lease_line(t["lease"]))
    step("A late message from the old lease version is refused (FR-8)")
    st, body = http("POST", "/escalations", {
        "type": "UNEXPECTED_EVENT", "message_id": "demo-stale", "lease_version": 1, "task_id": tid,
        "payload": {"what_happened": "old news", "where": gate, "robots_saw": ["x"]}})
    show("reply", f"HTTP {st} {body}")
    timeline(tid)
    notices("cluster")
    notices("operator")


def sc3(EdgeSim) -> None:
    title('SC-3  "Ward 2C is now isolation": hold routine, let urgent in, robots move the pumps out')
    text = "Ward 2C is now isolation. Get the spare pumps out, hold routine deliveries there, urgent ones can still go in."
    step('A routine delivery to 2C is already on its way')
    _, r0 = http("POST", "/requests", {"text": "Deliver a blanket to 2C, routine", "message_id": "demo-sc3-pre"})
    show("status", task(r0["task_id"])["status"])
    step(f'Staff: "{text}"')
    _, b = http("POST", "/requests", {"text": text, "message_id": "demo-sc3"})
    parent = task(b["task_id"])
    show("status", parent["status"])
    show("summary", parent["reason"])
    for cid in parent["children"] or []:
        c = task(cid)
        show(f"extraction {cid[-3:]}", f"{c['status']}  {c['spec']['item']} {c['spec']['source']} -> {c['spec']['destination']}  ({c['spec']['priority']})")
        if c["lease"] and c["status"] == "leased":
            say(f"{'':17}{lease_line(c['lease']).splitlines()[0]}")
    show("earlier routine", f"{task(r0['task_id'])['status']}  (stopped and held: its robot got a stop lease)")
    step("The west edge works through the extraction tasks; the pump count moves from the ward to the store")
    west = EdgeSim(API, "west-wing", complete=True)
    west.step()
    west.step()   # one west robot on this map: the second extraction starts only after the first ends
    show("places", http("GET", "/fleet")[1]["places"])
    show("extractions", [task(c)["status"] for c in parent["children"] or []])
    step("Routine request to 2C now: held. Urgent request to 2C: admitted")
    _, r = http("POST", "/requests", {"text": "Deliver a blanket to 2C, routine", "message_id": "demo-sc3-r"})
    _, u = http("POST", "/requests", {"text": "Deliver a blanket to 2C, urgent", "message_id": "demo-sc3-u"})
    show("routine", f"{task(r['task_id'])['status']}  {task(r['task_id']).get('reason') or ''}")
    show("urgent", f"{task(u['task_id'])['status']}  (exempt from the hold)")
    west.step()   # the urgent delivery finishes
    step('Staff: "Ward 2C is no longer in isolation." -> held work resumes')
    http("POST", "/requests", {"text": "Ward 2C is no longer in isolation.", "message_id": "demo-sc3-lift"})
    show("routine", f"{task(r['task_id'])['status']}   earlier routine: {task(r0['task_id'])['status']}")
    timeline(b["task_id"], 6)
    notices("place")
    notices("operator")


def main() -> None:
    global API
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenario", choices=["sc1", "sc2", "sc3", "all"], default="all")
    ap.add_argument("--pack", choices=["hospital_sim", "hospital"], default="hospital_sim")
    ap.add_argument("--mode", choices=["C1", "C2", "C3"], default="C1")
    ap.add_argument("--serve", action="store_true", help="keep the server running afterwards (port 8000 or $PORT)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    a = ap.parse_args()
    os.environ["NIMBUS_PACK"], os.environ["NIMBUS_MODE"] = a.pack, a.mode

    import server
    from edge_sim import EdgeSim

    data = Path(tempfile.mkdtemp(prefix="nimbus-demo-"))
    httpd = server.make_server("127.0.0.1", a.port if a.serve else 0, data)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    API = f"http://127.0.0.1:{httpd.server_address[1]}"
    fleet = http("GET", "/fleet")[1]
    say(f"{BOLD}Nimbus MS2 demo{OFF}  server {API}  pack={a.pack}  mode={a.mode}  model={fleet['llm']}  data={data}")

    try:
        if a.scenario in ("sc1", "all"):
            sc1(EdgeSim)
        if a.scenario in ("sc2", "all"):
            sc2(EdgeSim, a.pack)
        if a.scenario in ("sc3", "all"):
            sc3(EdgeSim)
        say(f"\n{GREEN}{BOLD}Done.{OFF} Cost counters: {http('GET', '/cost')[1]}")
        if a.serve:
            say(f"\n{BOLD}Server still running.{OFF} Open {API}/ (requester app) and {API}/operator (console).\n"
                f"Run a fake edge in another terminal:  python scripts/edge_sim.py --api {API} --cluster east-wing --complete\n"
                "Ctrl-C to stop.")
            while True:
                time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()


if __name__ == "__main__":
    main()
