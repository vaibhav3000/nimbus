#!/usr/bin/env bash
# Deterministic R9 launcher.  Full mode contains two measured 10-minute faults.
set -euo pipefail
case "${1:-}" in
  --smoke) mode=smoke ;;
  --full) mode=full ;;
  *) echo "Usage: $0 --smoke|--full" >&2; exit 2 ;;
esac
cd "$(dirname "$0")/.."
exec python3 experiments/robot_edge/r9_store_forward_reconnection.py --mode "$mode"
