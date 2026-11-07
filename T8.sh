#!/usr/bin/env bash
# =============================================================================
# T8.sh -- one-touch T8 "3 requests, 2 robots" scheduling contrast.
#
#   Two C3 implementations, same workload:
#     * C3 with EDF  (the PRD's C3: priority + earliest-deadline-first)
#     * C3 with FIFO (a deliberately naive classical baseline: arrival order)
#
#   Three requests hit one ward with a single usable robot; two queue. When the
#   robot frees, EDF serves the URGENT request (it makes its deadline) while
#   FIFO serves the routine that arrived first (the urgent then misses).
#
#   This is a DETERMINISTIC SIMULATION, not a Gazebo run, on purpose: the gap
#   only appears when two tasks are queued and compete for one freed robot under
#   deadline pressure, which needs a controlled clock and service time -- a live
#   Gazebo run cannot reproduce it reliably. It drives the REAL cloud
#   (Orchestrator + classical allocator + rule checker + scheduler); only robot
#   travel time is modelled. See scripts/run_t8.py.
#
#   The point: a classical baseline's success under contention depends heavily on
#   how well it is engineered (EDF vs FIFO) -- which is exactly the long-term
#   planning the LLM path brings. "C3" is not one thing.
#
# Usage:
#   ./T8.sh                       # EDF vs FIFO, 5-min service, 12-min urgent deadline
#   T8_SERVICE_MIN=6 ./T8.sh      # change the modelled per-delivery time
#
# Result is printed and saved to T8_results/.
# =============================================================================
set -uo pipefail
cd "$(dirname "$0")"
ROOT="$PWD"
RESULTS="$ROOT/T8_results"
mkdir -p "$RESULTS"
OUT="$RESULTS/t8_$(date -u +%Y%m%dT%H%M%SZ).txt"

echo "[T8] 3 requests / 2 robots -- EDF (PRD C3) vs FIFO (naive C3)"
echo "[T8] deterministic simulation of the real cloud (no Gazebo); see scripts/run_t8.py"
echo

PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 scripts/run_t8.py | tee "$OUT"
rc="${PIPESTATUS[0]}"

echo
echo "[T8] saved: ${OUT#$ROOT/}"
exit "$rc"
