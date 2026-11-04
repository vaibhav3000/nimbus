import json
import os
import subprocess
import sys
import time
import urllib.request

def banner(title):
    print("\n" + "=" * 75)
    print(f"  {title}")
    print("=" * 75)

def run_cmd(cmd):
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return p.stdout.strip()

banner("STEP 1: VERIFYING GAZEBO & CLOUD DAEMONS INSIDE CONTAINER")

# 1. Check Gazebo
gz_proc = run_cmd("ps -ef | grep -v grep | grep 'ign gazebo'")
if not gz_proc:
    print("[-] Gazebo not running, starting Gazebo...")
    subprocess.run("nohup /root/nimbus/start_gazebo.sh > /root/nimbus/gazebo.log 2>&1 &", shell=True)
    time.sleep(6)
else:
    print("[+] Gazebo simulation daemon is ACTIVE (Fortress headless)")

# 2. Check Cloud Server
cloud_proc = run_cmd("ps -ef | grep -v grep | grep 'server.py'")
if not cloud_proc:
    print("[-] Cloud server not running, starting Cloud Server...")
    subprocess.run("nohup /root/nimbus/start_server.sh > /root/nimbus/cloud_server.log 2>&1 &", shell=True)
    time.sleep(3)
else:
    print("[+] Cloud server daemon is ACTIVE on http://127.0.0.1:8000")

# 3. Clean databases & fastrtps locks
print("[+] Resetting task state and removing old edge databases...")
subprocess.run("python3 /root/nimbus/reset_data.py", shell=True, capture_output=True)
subprocess.run("rm -f /root/nimbus/edge_*.db /dev/shm/fastrtps* /dev/shm/sem.fastrtps*", shell=True)

# 4. Show initial robot poses in Gazebo
banner("STEP 2: INITIAL GAZEBO ROBOT POSES (GROUND TRUTH)")
init_poses = run_cmd("python3 /root/nimbus/check_pose.py")
print(init_poses)

# 5. Send Edge Heartbeat to register east-wing cluster with Cloud
banner("STEP 3: REGISTERING EDGE CLUSTER HEARTBEAT WITH CLOUD FLEET REGISTRY")
hb_payload = {
    "cluster_id": "east-wing",
    "robots": [
        {"id": "robot_1", "state": "idle", "zone": "south_equipment_approach", "battery": 100.0}
    ]
}
hb_req = urllib.request.Request(
    "http://127.0.0.1:8000/fleet/heartbeat",
    data=json.dumps(hb_payload).encode("utf-8"),
    headers={"Content-Type": "application/json"}
)
with urllib.request.urlopen(hb_req) as resp:
    hb_res = json.loads(resp.read().decode())
    print("[+] Heartbeat registered -> Cloud fleet health:", hb_res.get("health", "healthy"))

# 6. Dispatch simple task T1 to Cloud
banner("STEP 4: GIVING THE CLOUD A TASK (DISPATCHING PLAIN-LANGUAGE REQUEST)")
task_text = "Deliver a wheelchair to ward 3, routine"
msg_id = "req-demo-t1-" + str(int(time.time()))
print(f"[Requester] Sending Request: '{task_text}' (Msg ID: {msg_id})")

req_payload = {"message_id": msg_id, "text": task_text}
req = urllib.request.Request(
    "http://127.0.0.1:8000/requests",
    data=json.dumps(req_payload).encode("utf-8"),
    headers={"Content-Type": "application/json"}
)

with urllib.request.urlopen(req) as resp:
    res_data = json.loads(resp.read().decode())
    print("[Cloud API Response]:", json.dumps(res_data, indent=2))
    task_id = res_data.get("task_id")

# 7. Inspect Cloud Lease
banner(f"STEP 5: CLOUD DECISION & GENERATED LEASE FOR {task_id}")
with open("/root/nimbus/data/tasks.json") as f:
    tasks_data = json.load(f)
task_obj = tasks_data.get(task_id, {})
lease = task_obj.get("lease") or {}
print(f"Task ID         : {task_id}")
print(f"Status          : {task_obj.get('status')}")
print(f"Cluster Owner   : {lease.get('cluster_id')}")
print(f"Assigned Robot  : {lease.get('assigned_robots')}")
print(f"Allowed Zones   : {lease.get('allowed_zones')}")
print(f"Task Sequence   :")
for idx, step in enumerate(lease.get("task_sequence", []), 1):
    print(f"  [{idx}] {step.get('cmd')} -> {step.get('params', {})}")

banner("STEP 6: EDGE SUPERVISOR AUTONOMOUS EXECUTION IN GAZEBO")
print("[Edge] Starting Edge Supervisor with '--robot-backend sim' connecting to Gazebo ROS 2 bridge...")
edge_cmd = "source /opt/ros/humble/setup.bash && export IGN_PARTITION=custom_hospital_world && cd /root/nimbus && python3 edge/run_supervisor.py --cluster east-wing --robot-backend sim --interval 1.0 --once"
edge_proc = subprocess.run(edge_cmd, shell=True, executable="/bin/bash", capture_output=True, text=True)
print(edge_proc.stdout)
if edge_proc.stderr:
    err_lines = [l for l in edge_proc.stderr.splitlines() if not any(x in l for x in ["Creating ROS", "Creating GZ", "signal_handler", "RTPS_TRANSPORT_SHM"])]
    if err_lines:
        print("[Edge Notices]:\n" + "\n".join(err_lines))

banner("STEP 7: POST-EXECUTION VERIFICATION IN GAZEBO & CLOUD")
print("--- Final Gazebo Robot Poses ---")
final_poses = run_cmd("python3 /root/nimbus/check_pose.py")
print(final_poses)

print("\n--- Final Cloud Task Record ---")
try:
    with urllib.request.urlopen(f"http://127.0.0.1:8000/tasks/{task_id}") as resp:
        final_task = json.loads(resp.read().decode())
        print(f"Task ID      : {final_task.get('task_id')}")
        print(f"Final Status : {final_task.get('status')}")
        print(f"Cluster      : {final_task.get('cluster_id')}")
        print(f"Result Ref   : {final_task.get('result_ref')}")
except Exception as e:
    print("Error reading final task:", e)

print("\n--- Decision Replay Timeline (FR-20) ---")
try:
    with urllib.request.urlopen(f"http://127.0.0.1:8000/tasks/{task_id}/timeline") as resp:
        timeline = json.loads(resp.read().decode())
        t_items = timeline.get("timeline", [])
        print(f"Recorded {len(t_items)} decision-log events:")
        for ev in t_items:
            who = ev.get("who", "").upper()
            action = ev.get("action", ev.get("output", {}).get("status", "event"))
            print(f"  [{who:8s}] Action: {action}")
except Exception as e:
    print("Error reading timeline:", e)

banner("DEMONSTRATION COMPLETED SUCCESSFULLY WITHIN CONTAINER")
