#!/usr/bin/env bash
# Read-only diagnosis of "Final Mission Status: FAILED" on an edge supervisor (Gazebo sim backend).
# It changes nothing: it reads the edge's SQLite file, asks the cloud for its view, and checks the ROS topics.
#
#   cd <repo root>   (the folder you ran edge/run_supervisor.py from)
#   bash scripts/diagnose_edge.sh [cluster] [cloud_url] [robot_sim_name]
#   e.g. bash scripts/diagnose_edge.sh east-wing http://127.0.0.1:8000 robot_1
#
# Optional: NIMBUS_EDGE_TOKEN=... if the cloud requires the edge token; EDGE_DB=path/to/edge_x.db to override the DB path.

CLUSTER="${1:-east-wing}"
CLOUD="${2:-http://127.0.0.1:8000}"
ROBOT="${3:-robot_1}"
DB="${EDGE_DB:-edge_${CLUSTER//-/_}.db}"
TOKEN_HDR=()
[ -n "${NIMBUS_EDGE_TOKEN:-}" ] && TOKEN_HDR=(-H "Authorization: Bearer ${NIMBUS_EDGE_TOKEN}")

hr() { printf '\n=== %s ===\n' "$1"; }

hr "0. Tools (sqlite3 CLI is optional: python3's sqlite3 is used when it is missing)"
if ! command -v sqlite3 >/dev/null 2>&1; then
  echo "sqlite3 CLI not found; trying to install it (needs sudo on Linux; Homebrew on macOS)"
  if command -v apt-get >/dev/null 2>&1; then sudo apt-get install -y sqlite3 || echo "install failed; using python3 instead"
  elif command -v brew >/dev/null 2>&1; then brew install sqlite || echo "install failed; using python3 instead"
  else echo "no package manager found; using python3 instead"; fi
fi
command -v sqlite3 >/dev/null 2>&1 && echo "sqlite3: $(sqlite3 --version | cut -d' ' -f1)" || echo "sqlite3 CLI: not available (python3 fallback)"
command -v curl >/dev/null 2>&1 || { echo "curl is required (sudo apt-get install -y curl)"; exit 1; }
python3 --version

q() {   # q "<sql>": run a query against $DB, print rows with headers
  if command -v sqlite3 >/dev/null 2>&1; then sqlite3 -header -column "$DB" "$1"
  else python3 - "$DB" "$1" <<'PY'
import sqlite3, sys
cur = sqlite3.connect(sys.argv[1]).execute(sys.argv[2])
print(" | ".join(d[0] for d in cur.description))
for row in cur.fetchall(): print(" | ".join(str(v)[:300] for v in row))
PY
  fi
}

hr "1. Environment of THIS shell (the edge needs the same partition as the Gazebo launch)"
echo "IGN_PARTITION=${IGN_PARTITION:-<unset>}   GZ_PARTITION=${GZ_PARTITION:-<unset>}   ROS_DOMAIN_ID=${ROS_DOMAIN_ID:-<unset>}"
echo "Check your edge command started with IGN_PARTITION=... (a missing 'I' -> GN_PARTITION= is silently ignored)."
python3 -c "import rclpy; print('rclpy: importable')" 2>&1 | tail -1
ps aux | grep -E "run_supervisor|ros_gz_bridge|gz sim|ign gazebo" | grep -v grep | awk '{print "  running:", $11, $12, $13, $14, $15, $16}' | head -10

hr "2. THE ANSWER: the edge's recorded failure (decision_log EXECUTION_FAILED)"
if [ ! -f "$DB" ]; then
  echo "Edge DB '$DB' not found in $(pwd). Run this from the folder you started the edge in, or set EDGE_DB=<path>."
  ls -1 edge_*.db 2>/dev/null
else
  q "select decision_id, decision_action, substr(decision_output,1,900) as detail from decision_log where decision_action in ('EXECUTION_FAILED','ESCALATE_UNEXPECTED') order by rowid desc limit 5;"
  hr "2b. Last robot commands and their status"
  q "select cmd_id, robot_id, action, params_json, status from robot_commands order by rowid desc limit 8;"
  hr "2c. Leases the edge holds"
  q "select * from leases order by rowid desc limit 5;"
  hr "2d. Outbox (escalations waiting to reach the cloud; PENDING/FAILED here = the cloud never heard them)"
  q "select status, count(*) as n from outbox group by status;"
fi

hr "3. Cloud: reachable? what does it believe about this cluster?"
curl -s -m 5 "${TOKEN_HDR[@]}" "$CLOUD/fleet" | python3 -c "
import json,sys
try:
    for c in json.load(sys.stdin):
        print(c['cluster_id'], 'health=%s age_s=%s' % (c['health'], c['age_s']))
        for r in c['robots']: print('   ', r)
except Exception as e: print('cannot read /fleet:', e)"
echo "--- tasks and their last decisions"
curl -s -m 5 "$CLOUD/tasks" | python3 -c "
import json,sys
try:
    d = json.load(sys.stdin); d = d.get('tasks', d)
    for t in d[-5:]: print(t.get('task_id'), t.get('status'), (t.get('request_text') or '')[:70])
except Exception as e: print('cannot read /tasks:', e)"
echo "(timeline of one task:  curl -s $CLOUD/tasks/<task_id>/timeline | python3 -m json.tool)"

hr "4. ROS: are odometry and lidar alive for $ROBOT? (the real Gazebo path refuses to move on stale sensors)"
if command -v ros2 >/dev/null 2>&1; then
  ros2 topic list 2>/dev/null | grep "/$ROBOT/" | head -12 || true
  for topic in "/$ROBOT/odom" "/$ROBOT/ultrasonic/scan"; do
    echo "--- $topic (5 s sample; no output below means NO messages = stale)"
    python3 - "$topic" <<'PY'
import subprocess, sys
try:
    out = subprocess.run(["ros2", "topic", "hz", sys.argv[1]], capture_output=True, text=True, timeout=5).stdout
except subprocess.TimeoutExpired as e:
    out = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
print(out.strip().splitlines()[-1] if out.strip() else "  NO MESSAGES")
PY
  done
else
  echo "ros2 not on PATH: run 'source /opt/ros/<distro>/setup.bash' first, then re-run this script."
fi

hr "5. Reading guide"
cat <<'TXT'
 detail says...                                       meaning / fix
 odometry is stale or unavailable                     bridge not delivering /<robot>/odom: restart the bridge/launch; same ROS_DOMAIN_ID everywhere
 front range data is stale or unavailable/invalid     lidar topic not fresh: check section 4; the edge requires a message every 1 s
 front obstacle is inside safety distance (BLOCKED)   something within 0.25 m of the robot (another robot spawned next to it?)
 LOCALIZATION_UNAVAILABLE / no odometry ... timeout   edge started before the sim was ready, or wrong partition: start Gazebo first, then the edge
 Unknown destination / INVALID_DESTINATION            a zone name with no entry in sim_robot.py CLOUD_TO_SIM_ALIASES: use NIMBUS_PACK=hospital_sim
 Navigation timed out                                 robot never reached the point: check section 4 and the Gazebo window
 section 2d shows PENDING/FAILED outbox rows          the cloud never got the escalation: check the cloud URL and the edge token
Send P1/P3 the output of sections 2 and 4.
TXT
