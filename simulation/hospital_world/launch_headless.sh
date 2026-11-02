#!/usr/bin/env bash
# Launch the hospital server only. Pass extra Fortress arguments through.
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
command -v ign >/dev/null || { echo "Gazebo Fortress (ign) is not installed." >&2; exit 127; }
export IGN_GAZEBO_RESOURCE_PATH="$ROOT/models:$ROOT/models/robots:$ROOT/fuel_models${IGN_GAZEBO_RESOURCE_PATH:+:$IGN_GAZEBO_RESOURCE_PATH}"
export GZ_SIM_RESOURCE_PATH="$ROOT/models:$ROOT/models/robots:$ROOT/fuel_models${GZ_SIM_RESOURCE_PATH:+:$GZ_SIM_RESOURCE_PATH}"
export IGN_PARTITION="${HOSPITAL_IGN_PARTITION:-${IGN_PARTITION:-custom_hospital_world}}"
echo "Ignition transport partition: $IGN_PARTITION"
exec ign gazebo -r -s "$ROOT/hospital.sdf" "$@"
