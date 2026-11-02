#!/usr/bin/env bash
# Record the live custom_hospital_world GUI session after placing robot_1 at Ward 2B.
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
set +u
source /opt/ros/humble/setup.bash
set -u
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
export IGN_PARTITION="${IGN_PARTITION:-custom_hospital_world}"
exec python3 "$ROOT/scripts/record_live_cross_group_navigation.py" "$@"
