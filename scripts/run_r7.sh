#!/usr/bin/env bash
set -euo pipefail
cd "$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
exec python3 experiments/robot_edge/r7_safe_parking.py --scenario all --trials-a 20 --trials-b 10 --seed "${R7_SEED:-7}" --out results/final/R7
