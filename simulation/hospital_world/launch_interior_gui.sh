#!/usr/bin/env bash
# Launch the interior-friendly hospital with two wrapper-compatible RACECARs.
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
INTERIOR_WORLD="$ROOT/hospital_interior.sdf"
WORLD="$ROOT/hospital_interior_two_racecars.sdf"
ROBOT_MODEL="$ROOT/models/robots/mit_racecar/model.sdf"
command -v ign >/dev/null || { echo "Gazebo Fortress (ign) is not installed." >&2; exit 127; }
[[ -f "$ROBOT_MODEL" ]] || { echo "Vendored MIT RACECAR model not found: $ROBOT_MODEL" >&2; exit 1; }

python3 "$ROOT/scripts/generate_interior_world.py" --output "$INTERIOR_WORLD"
python3 "$ROOT/tools/generate_hospital_two_racecars.py" \
  --hospital "$INTERIOR_WORLD" --robot-model "$ROBOT_MODEL" --robots 2 --output "$WORLD"
export IGN_GAZEBO_RESOURCE_PATH="$ROOT/models:$ROOT/models/robots:$ROOT/fuel_models${IGN_GAZEBO_RESOURCE_PATH:+:$IGN_GAZEBO_RESOURCE_PATH}"
export GZ_SIM_RESOURCE_PATH="$ROOT/models:$ROOT/models/robots:$ROOT/fuel_models${GZ_SIM_RESOURCE_PATH:+:$GZ_SIM_RESOURCE_PATH}"
export IGN_PARTITION="${HOSPITAL_IGN_PARTITION:-${IGN_PARTITION:-custom_hospital_interior}}"
echo "Interior GUI: ceiling visual hidden; source ceiling collision retained."
echo "robot_1: pose (0.0, 12.0, 0), namespace /robot_1"
echo "robot_2: pose (0.0, 9.5, 0), namespace /robot_2"
echo "Ignition transport partition: $IGN_PARTITION"
exec ign gazebo -r "$WORLD" "$@"
