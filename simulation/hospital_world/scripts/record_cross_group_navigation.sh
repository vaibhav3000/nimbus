#!/usr/bin/env bash
# Record the actual Ward 2B -> Ward 2C navigation demo from robot_1's camera.
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
set +u  # ROS setup scripts read optional unset AMENT_* variables.
source /opt/ros/humble/setup.bash
set -u
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
exec python3 "$ROOT/scripts/record_cross_group_navigation.py" "$@"
