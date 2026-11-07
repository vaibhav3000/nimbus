#!/usr/bin/env bash
# One kinematic R7-A trial; does not start Gazebo.
set -euo pipefail
cd "$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
exec python3 experiments/robot_edge/r7_safe_parking.py --scenario A --trials-a 1 --seed "${R7_SEED:-7}" --out results/final/R7
