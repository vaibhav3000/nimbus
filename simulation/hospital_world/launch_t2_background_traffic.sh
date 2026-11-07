#!/usr/bin/env bash
# T2 only: generate the result-scoped world and run it with Gazebo's GUI.
set -euo pipefail
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)"
exec python3 "$ROOT/experiments/robot_edge/t2_background_traffic.py" --launch "$@"
