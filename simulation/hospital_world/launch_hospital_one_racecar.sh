#!/usr/bin/env bash
# Start the hospital with the first documented collision-free RACECAR spawn.
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
ROBOT_MODEL="$ROOT/models/robots/mit_racecar/model.sdf"
WORLD="$ROOT/hospital_one_racecar.sdf"
command -v ign >/dev/null || { echo "Gazebo Fortress (ign) is not installed." >&2; exit 127; }
[[ -f "$ROBOT_MODEL" ]] || { echo "Vendored MIT RACECAR model not found: $ROBOT_MODEL" >&2; exit 1; }

python3 "$ROOT/tools/generate_hospital_two_racecars.py" \
  --robot-model "$ROBOT_MODEL" --robots 1 --output "$WORLD"
export IGN_GAZEBO_RESOURCE_PATH="$ROOT/models:$ROOT/models/robots:$ROOT/fuel_models${IGN_GAZEBO_RESOURCE_PATH:+:$IGN_GAZEBO_RESOURCE_PATH}"
export GZ_SIM_RESOURCE_PATH="$ROOT/models:$ROOT/models/robots:$ROOT/fuel_models${GZ_SIM_RESOURCE_PATH:+:$GZ_SIM_RESOURCE_PATH}"
echo "robot_1: pose (0.0, 12.0, 0), namespace /robot_1"
if [[ "${HEADLESS:-0}" == "1" ]]; then
  unset DISPLAY
  exec ign gazebo -r -s --headless-rendering "$WORLD" "$@"
else
  exec ign gazebo -r "$WORLD" "$@"
fi
