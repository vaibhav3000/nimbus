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

  # Every robot receives the same local 50 m forward command. Its own
  # Gazebo-backed SafetySupervisor is the only obstacle-stop authority.
  python3 - "$ROOT" "$run" "$TMP_DIR/run-${run}.csv" <<'PY'
import csv
import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

root, run, output = Path(sys.argv[1]), int(sys.argv[2]), Path(sys.argv[3])
sys.path.insert(0, str(root))
from edge.mock.sim_robot import SimRobot

CASES = {
    "R1": ("forward_50m_direct", 50.0, 120.0),
    "R2": ("forward_50m_direct", 50.0, 120.0),
    "R3": ("forward_50m_direct", 50.0, 120.0),
    "R4": ("forward_50m_direct", 50.0, 120.0),
    "R5": ("forward_50m_direct", 50.0, 120.0),
    "R6": ("forward_50m_direct", 50.0, 120.0),
}

# These are local wrapper commands.  They do not call EdgeSupervisor,
# SimRobot.execute_command(), topology routing, or the navigation costmap.
# SafetySupervisor remains below RobotController and can stop any accepted
# forward command using the live Gazebo range sensor.
SPEED = 0.50
CONTROL_PERIOD = 0.12
MIN_TRAVEL_FOR_REFLEX = 0.05
COMPLETION_TOLERANCE = 0.35

def snapshot(robot):
    state = robot.runtime.state()
    truth = state.ground_truth
    if truth is None:
        return None
    pose = truth.pose
    velocity = state.odometry.velocity
    safety = robot.runtime.safety.status()
    return {
        "x": float(pose.x), "y": float(pose.y), "yaw": float(pose.yaw),
        "linear": float(velocity.linear), "angular": float(velocity.angular),
        "front": state.range.front, "minimum": state.range.minimum,
        "reflex_count": int(safety["reflex_stop_count"]),
        "reflex_reason": safety["reflex_stop_reason"],
        "controller": state.controller,
        "range_fresh": bool(state.health.range.fresh),
        "odom_fresh": bool(state.health.odometry.fresh),
    }

def settled(robot, timeout=5.0):
    """Confirm a zero command, an idle controller, and a stable Gazebo pose."""
    samples = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.10)
        current = snapshot(robot)
        if current is None or not (current["range_fresh"] and current["odom_fresh"]):
            return False, current
        samples.append(current)
        if len(samples) < 8:
            continue
        window = samples[-8:]
        x0, y0 = window[0]["x"], window[0]["y"]
        pose_span = max(math.hypot(item["x"] - x0, item["y"] - y0) for item in window)
        idle = not window[-1]["controller"].get("active_command_id")
        if idle and pose_span <= 0.02:
            return True, window[-1]
    return False, samples[-1] if samples else None

def direct_local_motion(rid):
    scenario, requested_distance, timeout = CASES[rid]
    robot = robots[rid]
    start = snapshot(robot)
    if start is None or not (start["range_fresh"] and start["odom_fresh"]):
        return {"run": run, "robot": rid, "scenario": scenario, "success": "false",
                "failure_reason": "live_gazebo_sensor_or_ground_truth_unavailable"}

    start_reflex_count = start["reflex_count"]
    started_at = time.monotonic()
    accepted = False
    moved = False
    last_detail = ""
    try:
        while time.monotonic() - started_at < timeout:
            current = snapshot(robot)
            if current is None or not (current["range_fresh"] and current["odom_fresh"]):
                return {"run": run, "robot": rid, "scenario": scenario, "success": "false",
                        "failure_reason": "live_gazebo_sensor_or_ground_truth_became_stale"}
            travelled = math.hypot(current["x"] - start["x"], current["y"] - start["y"])
            moved = moved or travelled >= MIN_TRAVEL_FOR_REFLEX

            # A reflex stop is valid only when it was caused by the live front
            # obstacle guard, after an accepted local forward command and
            # after the controller is idle and Gazebo ground truth is stable.
            if current["reflex_count"] > start_reflex_count:
                robot.runtime.motion.stop(source="r1_observation")
                stationary, final = settled(robot)
                reason = str(current["reflex_reason"] or "unknown safety reflex")
                front = "n/a" if current["front"] is None else f"{float(current['front']):.3f}m"
                if (reason == "front obstacle is inside safety distance" and accepted and moved and stationary):
                    return {"run": run, "robot": rid, "scenario": scenario, "success": "true",
                            "failure_reason": f"safety_reflex_stop; front={front}; final=({final['x']:.2f},{final['y']:.2f})"}
                return {"run": run, "robot": rid, "scenario": scenario, "success": "false",
                        "failure_reason": f"reflex_not_qualified; reason={reason}; accepted={accepted}; moved={moved}; stationary={stationary}; front={front}"}

            if travelled >= requested_distance - COMPLETION_TOLERANCE:
                robot.runtime.motion.stop(source="r1_distance_reached")
                stationary, final = settled(robot)
                if accepted and moved and stationary:
                    return {"run": run, "robot": rid, "scenario": scenario, "success": "true",
                            "failure_reason": f"completed_50m_safely; final=({final['x']:.2f},{final['y']:.2f})"}
                return {"run": run, "robot": rid, "scenario": scenario, "success": "false",
                        "failure_reason": "completed_distance_but_robot_did_not_settle"}

            # Direct 50 m forward wrapper command. No destination lookup,
            # route planner, map, edge supervisor, or cloud is involved.
            command = robot.runtime.motion.set_velocity(SPEED, 0.0, duration=CONTROL_PERIOD * 1.5, source="r1_local")
            status = str(command.status.value)
            accepted = accepted or status == "running"
            last_detail = str(command.reason or status)
            if status not in {"running", "completed"}:
                # A simultaneous safety reflex can make this particular send
                # reject; the next loop qualifies it from the watchdog state.
                time.sleep(CONTROL_PERIOD)
                continue
            time.sleep(CONTROL_PERIOD)
    finally:
        robot.runtime.motion.stop(source="r1_timeout_cleanup")

    return {"run": run, "robot": rid, "scenario": scenario, "success": "false",
            "failure_reason": f"direct_local_motion_timeout; accepted={accepted}; moved={moved}; last={last_detail}"}

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
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(direct_local_motion, rid) for rid in CASES]
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
