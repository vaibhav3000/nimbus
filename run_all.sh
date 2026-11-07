#!/usr/bin/env bash
# Start the whole Nimbus stack:  cloud (server.py)  +  edge supervisors (east-wing, west-wing)  +  simulator.
#
#   ./run_all.sh                 # auto: real Gazebo if `ign` + ROS 2 are present, else the kinematic stand-in
#   ./run_all.sh kinematic       # Gazebo-less: P1's map + SDK navigation, live viewer on :8090
#   ./run_all.sh gazebo          # real Gazebo Fortress (run inside the Docker image, see scripts/docker_gazebo.sh)
#   ./run_all.sh kinematic --not-quiet  # include detailed edge-to-robot commands in logs
#   ./run_all.sh kinematic --slm-mode ollama  # require a real local Ollama model
#   HEADLESS=1 ./run_all.sh gazebo   # Gazebo without a GUI window
#
# By default the requester and lease authority are the deployed cloud. Set
# NIMBUS_CLOUD_URL to a different deployment, or NIMBUS_LOCAL_CLOUD=1 for the
# in-repository cloud server.
# Stop with ./stop_all.sh . Logs go to logs/ .
set -uo pipefail
cd "$(dirname "$0")"
ROOT="$PWD"
MODE="auto"
EDGE_QUIET_ARGS=(--quiet)
SLM_MODE="${NIMBUS_SLM_MODE:-auto}"
SLM_MODEL="${NIMBUS_SLM_MODEL:-qwen2.5:1.5b}"
SYSTEM_MODE="${NIMBUS_MODE:-C1}"
while [ "$#" -gt 0 ]; do
  arg="$1"
  case "$arg" in
    auto|kinematic|gazebo)
      if [ "$MODE" != "auto" ]; then
        echo "!! simulator mode specified more than once: $MODE and $arg" >&2
        exit 2
      fi
      MODE="$arg"
      ;;
    --not-quiet)
      EDGE_QUIET_ARGS=()
      ;;
    --slm-mode)
      shift
      if [ "$#" -eq 0 ]; then
        echo "!! --slm-mode requires auto, ollama, llama_cpp, mock, or rules_only" >&2
        exit 2
      fi
      SLM_MODE="$1"
      ;;
    --slm-model)
      shift
      if [ "$#" -eq 0 ]; then
        echo "!! --slm-model requires an Ollama model name" >&2
        exit 2
      fi
      SLM_MODEL="$1"
      ;;
    --system-mode)
      shift
      if [ "$#" -eq 0 ]; then
        echo "!! --system-mode requires C1, C2, or C3" >&2
        exit 2
      fi
      SYSTEM_MODE="${1^^}"
      ;;
    *)
      echo "Usage: $0 [auto|kinematic|gazebo] [--not-quiet] [--system-mode C1|C2|C3] [--slm-mode auto|ollama|llama_cpp|mock|rules_only] [--slm-model MODEL]" >&2
      exit 2
      ;;
  esac
  shift
done
SYSTEM_MODE="${SYSTEM_MODE^^}"
case "$SLM_MODE" in
  auto|ollama|llama_cpp|mock|rules_only) ;;
  *) echo "!! invalid --slm-mode: $SLM_MODE" >&2; exit 2 ;;
esac
case "$SYSTEM_MODE" in
  C1|C2|C3) ;;
  *) echo "!! --system-mode must be C1, C2, or C3" >&2; exit 2 ;;
esac
export NIMBUS_MODE="$SYSTEM_MODE"
LOGS="${LOGS_DIR:-$ROOT/logs}"; PIDS="$LOGS/pids"; mkdir -p "$LOGS"; : > "$PIDS"
# Keep the existing logs/ directory as the live, latest-run view.  In
# addition, archive each process log under a unique run directory so evidence
# from an earlier Gazebo run is not overwritten by the next startup.
PERSISTENT_LOGS_ROOT="${PERSISTENT_LOGS_DIR:-$ROOT/run_logs}"
RUN_ID="${NIMBUS_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
RUN_LOGS="$PERSISTENT_LOGS_ROOT/$RUN_ID"
mkdir -p "$RUN_LOGS"
{
  echo "run_id=$RUN_ID"
  echo "started_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "requested_mode=$MODE"
  echo "system_mode=$SYSTEM_MODE"
  echo "command=$0 $*"
} > "$RUN_LOGS/run.env"

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
export NIMBUS_MINIMUM_FRONT_DISTANCE="${NIMBUS_MINIMUM_FRONT_DISTANCE:-0.30}"
export NIMBUS_SLM_MODE="$SLM_MODE"
export NIMBUS_SLM_MODEL="$SLM_MODEL"
DEPLOYED_CLOUD_URL="https://qrgdidp9te.execute-api.ap-south-1.amazonaws.com"
if [ "${NIMBUS_LOCAL_CLOUD:-0}" = 1 ]; then
  CLOUD="http://127.0.0.1:$PORT"
  USE_LOCAL_CLOUD=1
else
  CLOUD="${NIMBUS_CLOUD_URL:-$DEPLOYED_CLOUD_URL}"
  USE_LOCAL_CLOUD=0
fi
# C2/C3 need a cloud that runs that mode. The default deployed endpoint runs C1
# and its mode cannot be changed from here, so refuse to point a C2/C3 edge at
# it. A dedicated C2/C3 operator is fine: set NIMBUS_CLOUD_URL to its endpoint
# (then the edge talks to a cloud actually running that mode), or
# NIMBUS_LOCAL_CLOUD=1 for a local one.
if [ "$SYSTEM_MODE" != "C1" ] && [ "$USE_LOCAL_CLOUD" != 1 ]; then
  if [ "$CLOUD" = "$DEPLOYED_CLOUD_URL" ]; then
    echo "!! $SYSTEM_MODE cannot use the default deployed cloud: that endpoint runs C1 and its mode cannot be changed here." >&2
    echo "   Use NIMBUS_LOCAL_CLOUD=1 for a local cloud, or NIMBUS_CLOUD_URL=<a $SYSTEM_MODE endpoint> for a dedicated operator." >&2
    exit 2
  fi
  echo "== $SYSTEM_MODE against an explicitly set cloud: $CLOUD (make sure that endpoint runs $SYSTEM_MODE)"
fi
export NIMBUS_CLOUD_URL="$CLOUD"
# The deployed two-cluster registry names its assigned vehicles robot_1 and
# robot_2.  They map to the east/west Gazebo vehicles at the edge boundary.
[ "$USE_LOCAL_CLOUD" = 1 ] || export NIMBUS_CLOUD_ROBOT_PROFILE="deployed-two-cluster"
start() {
  local name="$1"
  shift
  # tee preserves the long-standing logs/<process>.log files while copying
  # identical stdout/stderr into this run's immutable archive.
  "$@" > >(tee "$LOGS/$name.log" "$RUN_LOGS/$name.log" >/dev/null) 2>&1 &
  echo $! >> "$PIDS"
  echo "   started $name (pid $!, log logs/$name.log; archive ${RUN_LOGS#$ROOT/}/$name.log)"
}
waitfor() { for _ in $(seq 1 "${3:-60}"); do curl -fs "$1" >/dev/null 2>&1 && return 0; sleep 1; done; echo "!! $2 did not come up (see logs/)"; return 1; }

LOGS_DIR="$LOGS" ./stop_all.sh quiet 2>/dev/null || true
: > "$PIDS"
# A run starts from clean state, but the previous run's decision log is FR-20
# evidence -- so rotate it aside instead of deleting it.  KEEP_DATA=1 reuses the
# existing state in place; NIMBUS_PRUNE_RUNS caps how many rotations are kept.
if [ "${KEEP_DATA:-0}" != 1 ]; then
  if [ -d "$DATA_DIR" ] && [ -n "$(ls -A "$DATA_DIR" 2>/dev/null)" ]; then
    _stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    _prev="${DATA_DIR%/}.prev/$_stamp"
    mkdir -p "$_prev"
    mv "$DATA_DIR"/* "$_prev"/ 2>/dev/null || true
    for _db in "$ROOT"/edge_*.db; do [ -e "$_db" ] && mv "$_db" "$_prev"/ 2>/dev/null || true; done
    echo "   previous run's data kept in ${_prev#$ROOT/}  (KEEP_DATA=1 to reuse in place)"
    # Keep the last N rotations so this cannot grow without bound.
    _keep="${NIMBUS_PRUNE_RUNS:-5}"
    ls -1d "${DATA_DIR%/}.prev"/*/ 2>/dev/null | head -n "-$_keep" | while read -r _old; do rm -rf "$_old"; done
  fi
  rm -rf "$DATA_DIR" "$ROOT"/edge_*.db
fi
mkdir -p "$DATA_DIR"

# `auto` falls back to mock for CI.  Real-inference runs must fail closed
# before any robot is started if Ollama or the selected model is unavailable.
if [ "$SLM_MODE" = ollama ]; then
  command -v ollama >/dev/null || { echo "!! Ollama is required for --slm-mode ollama" >&2; exit 1; }
  if ! curl -fsS --max-time 2 http://127.0.0.1:11434/api/tags >/dev/null; then
    echo "== starting Ollama"
    start ollama ollama serve
    waitfor http://127.0.0.1:11434/api/tags ollama 15 || exit 1
  fi
  if ! ollama show "$SLM_MODEL" >/dev/null 2>&1; then
    echo "!! Ollama model '$SLM_MODEL' is not installed. Run: ollama pull $SLM_MODEL" >&2
    ./stop_all.sh quiet 2>/dev/null || true
    exit 1
  fi
  echo "== edge SLM: Ollama model $SLM_MODEL (real inference required)"
else
  echo "== edge SLM mode: $SLM_MODE ($SLM_MODEL)"
fi

# --- 1. simulator --------------------------------------------------------------------------------
if [ "$MODE" = gazebo ]; then
  echo "== starting Gazebo (6 racecars)"
  start gazebo bash -c "cd simulation/hospital_world && exec ./launch_hospital_six_racecars.sh"
  echo "   waiting for the robots' odometry topics ..."
  ok=0; for _ in $(seq 1 120); do
    n=$(ign topic -l 2>/dev/null | grep -c "/robot_[1-6]/odom"); [ "$n" -ge 6 ] && { ok=1; break; }; sleep 1; done
  [ "$ok" = 1 ] && echo "   Gazebo is publishing all 6 robots." || echo "!! Gazebo topics not seen after 120 s: check logs/gazebo.log (continuing anyway)"
  SIM_ARGS=(--robot-backend sim --sim-mode gazebo --pose-source gazebo)
else
  echo "== starting kinematic viewer"
  start viewer python3 simulation/lite/viewer.py --port "${VIEWER_PORT:-8090}" --cloud "$CLOUD"
  export NIMBUS_SIM_VIEWER_URL="http://127.0.0.1:${VIEWER_PORT:-8090}"
  SIM_ARGS=(--robot-backend sim --sim-mode kinematic --pose-source odometry)
fi

# --- 2. cloud ------------------------------------------------------------------------------------
if [ "$USE_LOCAL_CLOUD" = 1 ]; then
  echo "== starting local cloud"
  start cloud python3 -u server.py
  waitfor "$CLOUD/fleet" cloud 30 || exit 1
else
  echo "== using deployed cloud: $CLOUD"
  waitfor "$CLOUD/fleet" deployed-cloud 30 || exit 1
fi

# --- 3. edge supervisors (one per wing) -----------------------------------------------------------
echo "== starting edge supervisors"
for c in east-wing west-wing; do
  start "edge_$c" python3 -u edge/run_supervisor.py --cluster "$c" --cloud-url "$CLOUD" --system-mode "$SYSTEM_MODE" "${SIM_ARGS[@]}" "${EDGE_QUIET_ARGS[@]}" --slm-mode "$SLM_MODE" --slm-model "$SLM_MODEL" --db "$DATA_DIR/edge_${c//-/_}.db"
done
sleep 6

echo
echo "================ Nimbus is up ($MODE) ================"
echo " System mode   : $SYSTEM_MODE"
echo " Requester app : $CLOUD/            (type: Need an infusion pump at 2B-12, urgent)"
echo " Operator      : $CLOUD/operator    Map: $CLOUD/map"
[ "$MODE" = kinematic ] && echo " Robots moving : http://127.0.0.1:${VIEWER_PORT:-8090}   (live map of the hospital)"
[ "$MODE" = gazebo ] && echo " Robots moving : in the Gazebo window (or HEADLESS=1: no window)"
echo " Scripted demo : python3 scripts/demo_e2e.py --api $CLOUD"
echo " Local cloud   : NIMBUS_LOCAL_CLOUD=1 ./run_all.sh $MODE"
echo " Run archive   : $RUN_LOGS"
echo " Stop          : ./stop_all.sh"
