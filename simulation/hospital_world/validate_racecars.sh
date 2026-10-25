#!/usr/bin/env bash
# Validate local RACECAR assets, namespaces, sensors, and a short motion step.
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$ROOT"
command -v ign >/dev/null || { echo "FAIL: Gazebo Fortress command 'ign' is unavailable." >&2; exit 127; }
[[ -f models/robots/mit_racecar/model.sdf ]] || { echo "FAIL: vendored RACECAR SDF is missing." >&2; exit 1; }

python3 tools/generate_hospital_two_racecars.py --robots 2 --output hospital_two_racecars.sdf
python3 - <<'PY'
from pathlib import Path
import xml.etree.ElementTree as ET

root = Path.cwd()
robot = root / "models/robots/mit_racecar/model.sdf"
model = ET.parse(robot).getroot().find("model")
if model is None:
    raise SystemExit("FAIL: RACECAR model SDF has no model element")
for uri in model.findall(".//mesh/uri"):
    text = uri.text or ""
    if not text.startswith("model://mit_racecar/"):
        raise SystemExit(f"FAIL: unexpected RACECAR mesh URI {text}")
    if not (root / "models/robots/mit_racecar" / text.removeprefix("model://mit_racecar/")).is_file():
        raise SystemExit(f"FAIL: missing RACECAR mesh {text}")
world = ET.parse(root / "hospital_two_racecars.sdf").getroot().find("world")
robots = {item.attrib.get("name"): item for item in world.findall("model")}
for name in ("robot_1", "robot_2"):
    item = robots.get(name)
    if item is None:
        raise SystemExit(f"FAIL: {name} missing from generated hospital world")
    expected = {f"/{name}/cmd_vel", f"/{name}/odom", f"/{name}/camera/image", f"/{name}/ultrasonic/scan"}
    found = {topic.text for topic in item.findall(".//topic") if topic.text}
    found.update(topic.text for topic in item.findall(".//odom_topic") if topic.text)
    if not expected.issubset(found):
        raise SystemExit(f"FAIL: {name} namespace rewrite missing {sorted(expected - found)}")
print("PASS: generated two local RACECAR models and all mesh/topic declarations resolve.")
PY

export IGN_GAZEBO_RESOURCE_PATH="$ROOT/models:$ROOT/models/robots:$ROOT/fuel_models${IGN_GAZEBO_RESOURCE_PATH:+:$IGN_GAZEBO_RESOURCE_PATH}"
export GZ_SIM_RESOURCE_PATH="$IGN_GAZEBO_RESOURCE_PATH"
export IGN_PARTITION="hospital_racecar_check_$$_$RANDOM"
LOG="$(mktemp)"
ign gazebo -r -s --headless-rendering "$ROOT/hospital_two_racecars.sdf" >"$LOG" 2>&1 &
SERVER_PID=$!
cleanup() {
  kill "$SERVER_PID" 2>/dev/null || true
  wait "$SERVER_PID" 2>/dev/null || true
  rm -f "$LOG"
}
trap cleanup EXIT
TOPICS=""
for _ in $(seq 1 15); do
  sleep 1
  TOPICS="$(ign topic -l)"
  if grep -Fx /robot_1/camera/image <<<"$TOPICS" >/dev/null && grep -Fx /robot_2/camera/image <<<"$TOPICS" >/dev/null; then
    break
  fi
done
for topic in /robot_1/odom /robot_1/camera/image /robot_1/ultrasonic/scan \
             /robot_2/odom /robot_2/camera/image /robot_2/ultrasonic/scan; do
  grep -Fx "$topic" <<<"$TOPICS" >/dev/null || { echo "FAIL: missing runtime topic $topic" >&2; exit 1; }
done
# A one-message subscription demonstrates that the sensor plugins publish;
# payloads stay out of stdout because camera messages are large.
timeout 6 ign topic -e -n 1 -t /robot_1/odom >/dev/null
timeout 6 ign topic -e -n 1 -t /robot_1/ultrasonic/scan >/dev/null
timeout 6 ign topic -e -n 1 -t /robot_1/camera/image >/dev/null
BEFORE="$(ign topic -e -n 1 -t /robot_1/odom)"
ign topic -t /robot_1/cmd_vel -m ignition.msgs.Twist -p 'linear { x: 0.10 } angular { z: 0.0 }'
sleep 2
ign topic -t /robot_1/cmd_vel -m ignition.msgs.Twist -p 'linear { x: 0.0 } angular { z: 0.0 }'
AFTER="$(ign topic -e -n 1 -t /robot_1/odom)"
position_x() { awk '/position \{/{inside=1; next} inside && /x:/{print $2; exit}'; }
BEFORE_X="$(printf '%s\n' "$BEFORE" | position_x)"
AFTER_X="$(printf '%s\n' "$AFTER" | position_x)"
awk -v before="$BEFORE_X" -v after="$AFTER_X" 'BEGIN {
  delta = after - before
  if (delta < 0.02) exit 1
}' || { echo "FAIL: robot_1 odometry did not advance after /robot_1/cmd_vel." >&2; exit 1; }
echo "PASS: both namespaced odometry, camera, and ultrasonic streams published; robot_1 moved from x=$BEFORE_X to x=$AFTER_X."
