#!/usr/bin/env bash
# Start the hospital with six local wrapper-compatible MIT RACECARs (R1..R6).
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
ROBOT_MODEL="$ROOT/models/robots/mit_racecar/model.sdf"
WORLD="$ROOT/hospital_six_racecars.sdf"
command -v ign >/dev/null || { echo "Gazebo Fortress (ign) is not installed." >&2; exit 127; }
[[ -f "$ROBOT_MODEL" ]] || { echo "Vendored MIT RACECAR model not found: $ROBOT_MODEL" >&2; exit 1; }

# The generated six-robot world is tracked and is the reproducible default.
# Regeneration is an explicit maintainer operation because the generator is
# not part of runtime deployment bundles.
if [[ "${NIMBUS_REGENERATE_WORLD:-0}" == "1" ]]; then
  GENERATOR="$ROOT/tools/generate_hospital_two_racecars.py"
  [[ -f "$GENERATOR" ]] || { echo "World generator not found: $GENERATOR" >&2; exit 1; }
  python3 "$GENERATOR" --robot-model "$ROBOT_MODEL" --robots 6 --output "$WORLD"
fi
export IGN_GAZEBO_RESOURCE_PATH="$ROOT/models:$ROOT/models/robots:$ROOT/fuel_models${IGN_GAZEBO_RESOURCE_PATH:+:$IGN_GAZEBO_RESOURCE_PATH}"
export GZ_SIM_RESOURCE_PATH="$ROOT/models:$ROOT/models/robots:$ROOT/fuel_models${GZ_SIM_RESOURCE_PATH:+:$GZ_SIM_RESOURCE_PATH}"
export IGN_PARTITION="${HOSPITAL_IGN_PARTITION:-${IGN_PARTITION:-custom_hospital_world}}"

echo "=================================================="
echo "Nimbus Hospital Simulation: Spawning 6 RACECARs"
echo "=================================================="
echo "R1 (robot_1): pose ( 0.0, 12.0, 0.0), namespace /robot_1 [Shared/East]"
echo "R2 (robot_2): pose ( 0.0, 10.0, 0.0), namespace /robot_2 [Shared/East]"
echo "R3 (robot_3): pose ( 4.5,  8.0, 0.0), namespace /robot_3 [East Wing]"
echo "R4 (robot_4): pose (-4.5,  8.0, 0.0), namespace /robot_4 [West Wing]"
echo "R5 (robot_5): pose (-4.5,  3.0, 0.0), namespace /robot_5 [West Wing]"
echo "R6 (robot_6): pose (-4.5, -8.0, 0.0), namespace /robot_6 [West Wing]"
echo "Ignition transport partition: $IGN_PARTITION"
echo "=================================================="

if [[ "${HEADLESS:-0}" == "1" ]]; then
  unset DISPLAY
  exec ign gazebo -r -s --headless-rendering "$WORLD" "$@"
else
  exec ign gazebo -r "$WORLD" "$@"
fi
