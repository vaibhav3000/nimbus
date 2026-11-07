#!/usr/bin/env bash
# R1 — Robot Safety Stop.  Five isolated, headless Gazebo runs; no edge/cloud.
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
WORLD="$ROOT/simulation/hospital_world/hospital_six_racecars.sdf"
RESULT_DIR="$ROOT/results/final/R1"
RESULTS="$RESULT_DIR/results.csv"
RUNS=5
TMP_DIR="$(mktemp -d -t nimbus-r1.XXXXXX)"
GAZEBO_PID=""
ACTIVE_PARTITION=""

command -v ign >/dev/null || { echo "Gazebo Fortress (ign) is required." >&2; exit 127; }
command -v python3 >/dev/null || { echo "python3 is required." >&2; exit 127; }
command -v setsid >/dev/null || { echo "setsid is required for isolated Gazebo cleanup." >&2; exit 127; }
[[ -f "$WORLD" ]] || { echo "Missing six-robot hospital world: $WORLD" >&2; exit 1; }

# This test is strictly robot-local.  It neither starts nor contacts Nimbus
# cloud/edge services; remove any inherited endpoint/token configuration.
unset NIMBUS_CLOUD_URL NIMBUS_ADMIN_TOKEN NIMBUS_EDGE_TOKEN
export NIMBUS_GOAL_TIMEOUT="${NIMBUS_GOAL_TIMEOUT:-90}"
export IGN_GAZEBO_RESOURCE_PATH="$ROOT/simulation/hospital_world/models:$ROOT/simulation/hospital_world/models/robots:$ROOT/simulation/hospital_world/fuel_models${IGN_GAZEBO_RESOURCE_PATH:+:$IGN_GAZEBO_RESOURCE_PATH}"
export GZ_SIM_RESOURCE_PATH="$IGN_GAZEBO_RESOURCE_PATH"

partition_processes() {
  # Bridges are spawned in their own sessions by GazeboAdapter, so they are
  # not necessarily descendants of `ign gazebo`.  Restrict discovery to this
  # run's unique partition and known simulation/bridge commands; never touch
  # another Gazebo session.
  local proc pid cmd
  [[ -n "$ACTIVE_PARTITION" ]] || return 0
  for proc in /proc/[0-9]*; do
    pid="${proc##*/}"
    [[ "$pid" != "$$" && "$pid" != "$PPID" && -r "$proc/environ" && -r "$proc/cmdline" ]] || continue
    if ! tr '\0' '\n' <"$proc/environ" 2>/dev/null | grep -Fqx "IGN_PARTITION=$ACTIVE_PARTITION" \
      && ! tr '\0' '\n' <"$proc/environ" 2>/dev/null | grep -Fqx "GZ_PARTITION=$ACTIVE_PARTITION"; then
      continue
    fi
    cmd="$(tr '\0' ' ' <"$proc/cmdline" 2>/dev/null || true)"
    case "$cmd" in
      *"ign gazebo"*|*"ignition-gazebo"*|*"gz sim"*|*"ros_gz_bridge"*|*"ros_gz_image"*)
        printf '%s\n' "$pid"
        ;;
    esac
  done
}

stop_iteration() {
  local pids=() deadline

  # Stop the Gazebo process group first. `setsid` below makes this group
  # exclusive to one R1 iteration, including any Gazebo descendants.
  if [[ -n "$GAZEBO_PID" ]] && kill -0 "$GAZEBO_PID" 2>/dev/null; then
    kill -TERM -- "-$GAZEBO_PID" 2>/dev/null || true
    deadline=$((SECONDS + 8))
    while kill -0 "$GAZEBO_PID" 2>/dev/null && (( SECONDS < deadline )); do sleep 1; done
    kill -KILL -- "-$GAZEBO_PID" 2>/dev/null || true
    wait "$GAZEBO_PID" 2>/dev/null || true
  fi

  # Then reap partition-scoped ROS-Gazebo bridges left after their SDK close.
  mapfile -t pids < <(partition_processes)
  if (( ${#pids[@]} )); then
    echo "[R1] cleaning ${#pids[@]} residual Gazebo/bridge process(es) in $ACTIVE_PARTITION"
    kill -TERM "${pids[@]}" 2>/dev/null || true
    deadline=$((SECONDS + 8))
    while (( SECONDS < deadline )); do
      mapfile -t pids < <(partition_processes)
      (( ${#pids[@]} == 0 )) && break
      sleep 1
    done
    mapfile -t pids < <(partition_processes)
    (( ${#pids[@]} == 0 )) || kill -KILL "${pids[@]}" 2>/dev/null || true
  fi
  GAZEBO_PID=""
}

cleanup() {
  stop_iteration
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT INT TERM

wait_for_world() {
  local deadline=$((SECONDS + 45))
  local topics
  while (( SECONDS < deadline )); do
    topics="$(ign topic -l 2>/dev/null || true)"
    local ready=1 robot
    for robot in 1 2 3 4 5 6; do
      [[ "$topics" == *"/robot_${robot}/odom"* ]] || ready=0
      [[ "$topics" == *"/robot_${robot}/ultrasonic/scan"* ]] || ready=0
    done
    (( ready )) && return 0
    sleep 1
  done
  return 1
}

mkdir -p "$RESULT_DIR"
printf 'run,robot,scenario,success,failure_reason\n' >"$RESULTS"

for run in $(seq 1 "$RUNS"); do
  export IGN_PARTITION="nimbus_r1_${$}_${run}"
  export HOSPITAL_IGN_PARTITION="$IGN_PARTITION"
  export GZ_PARTITION="$IGN_PARTITION"
  ACTIVE_PARTITION="$IGN_PARTITION"
  unset DISPLAY
  GAZEBO_LOG="$TMP_DIR/gazebo-${run}.log"
  echo "[R1] run $run/$RUNS: starting isolated headless hospital world ($IGN_PARTITION)"
  setsid ign gazebo -r -s --headless-rendering "$WORLD" >"$GAZEBO_LOG" 2>&1 &
  GAZEBO_PID=$!

  if ! wait_for_world; then
    echo "[R1] Gazebo topics did not become ready; recording six failures." >&2
    for robot in R1 R2 R3 R4 R5 R6; do
      printf '%s,%s,world_startup,false,gazebo_topics_not_ready\n' "$run" "$robot" >>"$RESULTS"
    done
    stop_iteration
    continue
  fi

  # Actual SDF/map-derived targets, expressed in Gazebo world metres:
  # R1 starts at (0.0, 12.0), faces +Y, and the static occupancy map's first
  # north wall cell on that ray is y=18.7; y=19.7 is one metre beyond it.
  # The nurse-station approach is topology.json's (0.1, 5.1), not the desk mesh.
  # R5/R6 start with yaw 0, so forward is +X: (-4.5,3.0)+5m=(0.5,3.0),
  # and (-4.5,-8.0)+50m=(45.5,-8.0).
  python3 - "$ROOT" "$run" "$TMP_DIR/run-${run}.csv" <<'PY'
import csv
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

root, run, output = Path(sys.argv[1]), int(sys.argv[2]), Path(sys.argv[3])
sys.path.insert(0, str(root))
from edge.mock.sim_robot import SimRobot

CASES = {
    "R1": ("north_wall_plus_1m", (0.1, 19.7), True),
    "R2": ("nurse_station_approach", (0.1, 5.1), False),
    "R3": ("nurse_station_approach", (0.1, 5.1), False),
    "R4": ("nurse_station_approach", (0.1, 5.1), False),
    "R5": ("forward_5m", (0.5, 3.0), False),
    "R6": ("forward_50m", (45.5, -8.0), False),
}

robots = {rid: SimRobot(rid, use_gazebo=True, validation_source="ground_truth", pose_source="gazebo") for rid in CASES}
rows = []
try:
    deadline = time.monotonic() + 45
    not_ready = set(robots)
    while not_ready and time.monotonic() < deadline:
        not_ready = {rid for rid in not_ready if robots[rid].get_status().get("connection_state") != "READY"}
        time.sleep(0.1)
    if not_ready:
        rows = [{"run": run, "robot": rid, "scenario": CASES[rid][0], "success": "false",
                 "failure_reason": "robot_local_sensors_not_ready"} for rid in CASES]
    else:
        def command(rid):
            scenario, target, wall_expected = CASES[rid]
            result = robots[rid].execute_command({
                "cmd_id": f"r1-{run}-{rid}", "action": "go_to",
                "params": {"target": list(target), "target_frame": "gazebo/world", "speed": 0.35},
            })
            status = str(result.get("status", "FAILED")).upper()
            detail = str(result.get("message", "no terminal reason")).replace(",", ";").replace("\n", " ")
            arrived = bool(result.get("arrival_verified"))
            if wall_expected:
                success = status == "BLOCKED"
                reason = "safe_stop_before_north_wall" if success else f"expected_safe_stop; got_{status.lower()}: {detail}"
            elif status == "COMPLETED" and arrived:
                success, reason = True, "completed_safely"
            elif status == "BLOCKED":
                # BLOCKED is the SDK's safety-controller terminal outcome.
                # TIMEOUT/FAILED/INVALID_DESTINATION are deliberately failures.
                success, reason = True, f"safe_stop: {detail}"
            else:
                success, reason = False, f"{status.lower()}: {detail}"
            return {"run": run, "robot": rid, "scenario": scenario,
                    "success": str(success).lower(), "failure_reason": reason}

        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(command, rid) for rid in CASES]
            rows = [future.result() for future in as_completed(futures)]
finally:
    for robot in robots.values():
        robot.close()

with output.open("w", newline="") as handle:
    writer = csv.DictWriter(handle, fieldnames=["run", "robot", "scenario", "success", "failure_reason"])
    writer.writeheader(); writer.writerows(sorted(rows, key=lambda row: row["robot"]))
PY
  tail -n +2 "$TMP_DIR/run-${run}.csv" >>"$RESULTS"
  # Do not begin the next iteration until this world and its partition-scoped
  # ROS-Gazebo bridges have exited.
  stop_iteration
done

successes=$(awk -F, 'NR > 1 && $4 == "true" { count++ } END { print count + 0 }' "$RESULTS")
failures=$((RUNS * 6 - successes))
echo "[R1] complete: ${successes} successes, ${failures} failures"
echo "[R1] CSV: $RESULTS"
