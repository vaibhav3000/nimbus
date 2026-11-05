#!/usr/bin/env bash
# Start the whole Nimbus stack:  cloud (server.py)  +  edge supervisors (east-wing, west-wing)  +  simulator.
#
#   ./run_all.sh                 # auto: real Gazebo if `ign` + ROS 2 are present, else the kinematic stand-in
#   ./run_all.sh kinematic       # Gazebo-less: P1's map + SDK navigation, live viewer on :8090
#   ./run_all.sh gazebo          # real Gazebo Fortress (run inside the Docker image, see scripts/docker_gazebo.sh)
#   HEADLESS=1 ./run_all.sh gazebo   # Gazebo without a GUI window
#
# URLs: requester app http://127.0.0.1:8000   operator console /operator   map /map   kinematic viewer http://127.0.0.1:8090
# Stop with ./stop_all.sh . Logs go to logs/ .
set -uo pipefail
cd "$(dirname "$0")"
ROOT="$PWD"
MODE="${1:-auto}"
LOGS="${LOGS_DIR:-$ROOT/logs}"; PIDS="$LOGS/pids"; mkdir -p "$LOGS"; : > "$PIDS"

# --- environment (ROS 2 if present) -------------------------------------------------------------
for f in /opt/ros/*/setup.bash; do [ -f "$f" ] && { set +u; source "$f"; set -u; break; }; done
[ -f /root/addModelPath.sh ] && { set +u; source /root/addModelPath.sh; set -u; }
python3 -c "import jsonschema" 2>/dev/null || pip install -q jsonschema 2>&1 | tail -1

HAVE_GZ=0; command -v ign >/dev/null && python3 -c "import rclpy" 2>/dev/null && HAVE_GZ=1
if [ "$MODE" = auto ]; then [ "$HAVE_GZ" = 1 ] && MODE=gazebo || MODE=kinematic; fi
if [ "$MODE" = gazebo ] && [ "$HAVE_GZ" = 0 ]; then
  echo "!! Gazebo/ROS 2 not found here -> falling back to the kinematic simulator (use scripts/docker_gazebo.sh for real Gazebo)"; MODE=kinematic; fi
echo "== Nimbus: simulator mode = $MODE"

export NIMBUS_PACK="${NIMBUS_PACK:-hospital_sim6}"   # P1's six-racecar hospital map, robots R1-R6
export DATA_DIR="${DATA_DIR:-$ROOT/data}"
export PORT="${PORT:-8000}"
export IGN_PARTITION="${IGN_PARTITION:-custom_hospital_world}"; export GZ_PARTITION="$IGN_PARTITION"
export NIMBUS_SENSOR_TIMEOUT="${NIMBUS_SENSOR_TIMEOUT:-5.0}"
export NIMBUS_CAMERA_TIMEOUT="${NIMBUS_CAMERA_TIMEOUT:-10.0}"
export NIMBUS_GOAL_TIMEOUT="${NIMBUS_GOAL_TIMEOUT:-600.0}"
export NIMBUS_MINIMUM_FRONT_DISTANCE="${NIMBUS_MINIMUM_FRONT_DISTANCE:-0.20}"
CLOUD="http://127.0.0.1:$PORT"
start() { local name="$1"; shift; "$@" > "$LOGS/$name.log" 2>&1 & echo $! >> "$PIDS"; echo "   started $name (pid $!, log logs/$name.log)"; }
waitfor() { for _ in $(seq 1 "${3:-60}"); do curl -fs "$1" >/dev/null 2>&1 && return 0; sleep 1; done; echo "!! $2 did not come up (see logs/)"; return 1; }

LOGS_DIR="$LOGS" ./stop_all.sh quiet 2>/dev/null || true
: > "$PIDS"
[ "${KEEP_DATA:-0}" = 1 ] || rm -rf "$DATA_DIR" "$ROOT"/edge_*.db
mkdir -p "$DATA_DIR"

# --- 1. simulator --------------------------------------------------------------------------------
if [ "$MODE" = gazebo ]; then
  echo "== starting Gazebo (6 racecars)"
  start gazebo bash -c "cd simulation/hospital_world && exec ./launch_hospital_six_racecars.sh"
  echo "   waiting for the robots' odometry topics ..."
  ok=0; for _ in $(seq 1 120); do
    n=$(ign topic -l 2>/dev/null | grep -c "/robot_[1-6]/odom"); [ "$n" -ge 6 ] && { ok=1; break; }; sleep 1; done
  [ "$ok" = 1 ] && echo "   Gazebo is publishing all 6 robots." || echo "!! Gazebo topics not seen after 120 s: check logs/gazebo.log (continuing anyway)"
  SIM_ARGS=(--robot-backend sim --sim-mode gazebo --gazebo-validation odometry)
else
  echo "== starting kinematic viewer"
  start viewer python3 simulation/lite/viewer.py --port "${VIEWER_PORT:-8090}" --cloud "$CLOUD"
  export NIMBUS_SIM_VIEWER_URL="http://127.0.0.1:${VIEWER_PORT:-8090}"
  SIM_ARGS=(--robot-backend sim --sim-mode kinematic)
fi

# --- 2. cloud ------------------------------------------------------------------------------------
echo "== starting cloud"
start cloud python3 -u server.py
waitfor "$CLOUD/fleet" cloud 30 || exit 1

# --- 3. edge supervisors (one per wing) -----------------------------------------------------------
echo "== starting edge supervisors"
for c in east-wing west-wing; do
  start "edge_$c" python3 -u edge/run_supervisor.py --cluster "$c" --cloud-url "$CLOUD" "${SIM_ARGS[@]}" --quiet --db "$DATA_DIR/edge_${c//-/_}.db"
done
sleep 6

echo
echo "================ Nimbus is up ($MODE) ================"
echo " Requester app : $CLOUD/            (type: Need an infusion pump at 2B-12, urgent)"
echo " Operator      : $CLOUD/operator    Map: $CLOUD/map"
[ "$MODE" = kinematic ] && echo " Robots moving : http://127.0.0.1:${VIEWER_PORT:-8090}   (live map of the hospital)"
[ "$MODE" = gazebo ] && echo " Robots moving : in the Gazebo window (or HEADLESS=1: no window)"
echo " Scripted demo : python3 scripts/demo_e2e.py --api $CLOUD"
echo " Stop          : ./stop_all.sh"
