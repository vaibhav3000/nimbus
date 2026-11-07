#!/usr/bin/env python3
"""Manual R7 harness: kinematic WAN/expiry safe-parking evidence (FR-6/FR-10)."""
from __future__ import annotations
import argparse, csv, json, random, sys, threading, time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from edge.supervisor.supervisor import EdgeSupervisor
from experiments.cloud import lib

ROUTE = ["north_hall", "east_north_gate", "east_waiting_approach", "east_mid_hall", "east_south_hall", "east_south_hall_corner", "south_equipment_approach"]

class Recorder(BaseHTTPRequestHandler):
    received: list[dict] = []
    def do_POST(self):
        size=int(self.headers.get("Content-Length",0)); body=json.loads(self.rfile.read(size) or b"{}")
        self.received.append({"path":self.path,"payload":body,"timestamp":time.time()})
        self.send_response(201); self.end_headers()
    def log_message(self,*_): pass

def lease(trial: int, expiry_s: float) -> dict:
    now=datetime.now(timezone.utc)
    # Nimbus requires deadline_ts <= expiry_ts.  R7-B uses a deterministic
    # short expiry to occur during the long kinematic route; it is not random.
    deadline_s = 90.0 if expiry_s >= 120.0 else 3.0
    return {"lease_id":f"lease-r7-{trial}-v1","version":1,"priority":"urgent",
      "deadline_ts":(now+timedelta(seconds=deadline_s)).strftime("%Y-%m-%dT%H:%M:%SZ"),
      "expiry_ts":(now+timedelta(seconds=expiry_s)).strftime("%Y-%m-%dT%H:%M:%SZ"),
      "allowed_zones":ROUTE,"forbidden_zones":[],"speed_limit":1.0,"assigned_robots":["R1"],"cluster_id":"east-wing",
      "when_to_escalate":["robot_failed"],"task_sequence":[{"cmd":"go_to","robot":"R1","params":{"to":"south_equipment_approach","speed":1.0,"via":ROUTE}}]}

def run_trial(kind: str, trial: int, rng: random.Random, root: Path) -> tuple[dict,list[dict]]:
    Recorder.received=[]; server=ThreadingHTTPServer(("127.0.0.1",0),Recorder); threading.Thread(target=server.serve_forever,daemon=True).start()
    expiry_s=5.0 if kind=="B" else 120.0
    sup=EdgeSupervisor(cluster_id="east-wing",cloud_url=f"http://127.0.0.1:{server.server_port}",db_path=str(root/f"trial-{kind}-{trial}.db"),
                       robot_backend="sim",sim_mode="kinematic",slm_mode="mock",verbose=False)
    robot=sup.robots["R1"]; events=[]; started=time.time(); fault_at=started+rng.uniform(.25,.75) if kind=="A" else started
    # SimRobot clamps command speed to topology waypoint caps.  Raise those
    # caps for this R7-only kinematic experiment without changing the shared
    # hospital topology used by other scenarios.
    for zone in ROUTE:
        if zone in sup.topology.get("nodes", {}):
            sup.topology["nodes"][zone]["speed_cap"] = 1.0
        if zone in robot.topology_nodes:
            robot.topology_nodes[zone]["speed_cap"] = 1.0
    original=robot.execute_command
    def observed(cmd):
        events.append({"event":"command_start","time":time.time(),"action":cmd.get("action"),"params":cmd.get("params",{}),"pose":list(robot.pose)})
        result=original(cmd); events.append({"event":"command_end","time":time.time(),"action":cmd.get("action"),"status":result.get("status"),"pose":list(robot.pose)})
        return result
    robot.execute_command=observed
    timers=[]
    if kind=="A":
        def outage():
            sup.set_network_available(False); events.append({"event":"wan_down","time":time.time(),"pose":list(robot.pose)})
            sup.trigger_unexpected_event("r7", "WAN disconnected during navigation", "east_mid_hall", ["R1"], 1, robot.current_zone)
        def recover():
            summary=sup.simulate_network_recovery(); drained=sup.flush_outbox(timeout=5); events.append({"event":"wan_up","time":time.time(),"drained":drained,"summary":summary,"pose":list(robot.pose)})
        timers=[threading.Timer(fault_at-started,outage),threading.Timer(fault_at-started+1.0,recover)]
        [t.start() for t in timers]
    payload=lease(trial,expiry_s); expiry=datetime.fromisoformat(payload["expiry_ts"].replace("Z","+00:00")).timestamp()
    try: result=sup.process_lease(payload)
    finally:
        [t.cancel() for t in timers]; sup.stop_outbox_worker(); robot.close(); server.shutdown(); server.server_close()
    history=list(robot.history); parks=[h for h in history if h.get("params",{}).get("reason")=="lease_expiry_safe_park"]
    park=parks[-1] if parks else None; park_name=(park or {}).get("params",{}).get("to")
    task_after_expiry=kind=="B" and any(h.get("action")=="go_to" and h.get("params",{}).get("to")=="south_equipment_approach" and h.get("end_time",0)>expiry for h in history)
    safe=bool(park_name and sup.parking_engine.is_safe_for_parking(park_name))
    row={"scenario":"R7-"+kind,"trial":trial,"seed":rng.randrange(2**31),"status":result.get("status"),"final_x":robot.pose[0],"final_y":robot.pose[1],"parking_zone":park_name,"parking_safe":safe,"time_to_park_s":None if not park else round(park.get("end_time",time.time())-expiry,3),"unauthorized_post_expiry_motion":task_after_expiry,"buffered_delivered":kind!="A" or bool(Recorder.received),"pass":(kind=="A" and bool(Recorder.received)) or (kind=="B" and safe and not task_after_expiry)}
    return row, events+[{"event":"pose","time":t,"x":x,"y":y,"yaw":yaw,"status":st} for x,y,yaw,t,st in robot.position_history]

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--scenario",choices=["A","B","all"],default="all"); ap.add_argument("--trials-a",type=int,default=20); ap.add_argument("--trials-b",type=int,default=10); ap.add_argument("--seed",type=int,default=7); ap.add_argument("--out",default="results/final/R7"); a=ap.parse_args()
    if Path(a.out).resolve() != (ROOT/"results/final/R7").resolve(): raise ValueError("--out must be results/final/R7")
    rd=lib.ResultDir("R7","WAN disconnection and lease-expiry parking",None,["--scenario",a.scenario,"--seed",str(a.seed),"--out",a.out],prd="FR-6, FR-10"); rd.reset_raw(); rd.env.update({"source":"measured","model":"SimRobot kinematic"})
    rng=random.Random(a.seed); rows=[]
    plan=([("A",a.trials_a)] if a.scenario=="A" else [("B",a.trials_b)] if a.scenario=="B" else [("A",a.trials_a),("B",a.trials_b)])
    for kind,count in plan:
      for trial in range(1,count+1):
       row,events=run_trial(kind,trial,rng,rd.dir/"raw"); rows.append(row); rd.raw("events",{"scenario":row["scenario"],"trial":trial,"events":events})
       with (rd.dir/"raw"/f"poses-{kind}-{trial:02d}.jsonl").open("w") as f:
        for e in events:
         if e["event"]=="pose": f.write(json.dumps(e)+"\n")
    with (rd.dir/"parking.csv").open("w",newline="") as f: w=csv.DictWriter(f,fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    rd.summary(rows); passed=sum(bool(r["pass"]) for r in rows); rd.finish(claim="Kinematic R7 records WAN outbox replay and expiry parking.",pass_bar="No unsafe park, no post-expiry task motion, buffered WAN events delivered.",verdict="PASS" if passed==len(rows) else "FAIL",measured=f"{passed}/{len(rows)} trials passed; inspect parking.csv and raw pose/event logs.",notes=["R7-B currently exposes whether expiry interrupts an active navigation leg; it is not assumed to pass.","No Gazebo is started."])
if __name__=="__main__": main()
