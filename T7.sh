#!/usr/bin/env bash
# =============================================================================
# T7.sh -- one-touch T7: "Inventory sweep: survey idle pumps".  Metric: WAN bandwidth.
#
#   A real survey (inventory sweep of idle infusion pumps) is planned by the cloud and
#   executed by the KINEMATIC edge (scripts/edge_sim.py, unmodified). The metric is how
#   many bytes the edge must send over the WAN to report the sweep -- and that depends on
#   how smart the edge is:
#     C1 (edge SLM)      -> ships a filtered DIGEST            -> LEAST WAN bytes
#     C2 (cloud relay)   -> ships RAW observations             -> MOST WAN bytes
#     C3 (classical edge)-> ships STRUCTURED counts            -> in between
#
#   Cloud (project rule -- C1/C2 ALWAYS use the deployed endpoints):
#     C1 -> deployed qrgdidp9te,  C2 -> deployed xkczmnuqla,  C3 -> local classical cloud.
#
#   SCOPE: this experiment is scoped to T7 ONLY -- scripts/edge_sim.py and every other
#   shared file are UNCHANGED (the per-config "edge filter" is modelled and measured in
#   scripts/run_t7.py). Only STANDARD survey requests + normal edge_sim execution are sent
#   to the deployed cloud; the per-config report size is measured in the harness, never
#   posted as custom data the cloud might reject.
#
# Usage:
#   ./T7.sh                        # C1 C2 C3, one pass
#   T7_MODES="C1 C2" T7_SEEDS="1 2" ./T7.sh
#   CLOUD_C1=... CLOUD_C2=... ./T7.sh
# =============================================================================
set -uo pipefail
cd "$(dirname "$0")"
ROOT="$PWD"

MODES=(${T7_MODES:-C1 C2 C3})
SEEDS=(${T7_SEEDS:-1})
CLOUD_C1="${CLOUD_C1:-https://qrgdidp9te.execute-api.ap-south-1.amazonaws.com}"
CLOUD_C2="${CLOUD_C2:-https://xkczmnuqla.execute-api.ap-south-1.amazonaws.com}"

say() { printf '\n\033[1m[T7] %s\033[0m\n' "$*"; }

say "sweep: modes=[${MODES[*]}] seeds=[${SEEDS[*]}] (C1/C2 deployed, C3 local; kinematic edge_sim)"

# Reset FR-19 long-term memory on BOTH deployed clouds up front (determinism).
say "resetting long-term memory on deployed C1 and C2"
for url in "$CLOUD_C1" "$CLOUD_C2"; do
  if curl -fsS -m 25 -X POST "${url%/}/reset-memory" -H 'Content-Type: application/json' -d '{}' >/dev/null 2>&1; then
    echo "  reset ok   : ${url%/}"
  else
    echo "  reset FAILED: ${url%/} (continuing; run_t7.py resets again per trial)"
  fi
done

say "clearing results/final/T7/ for a clean run"
rm -rf "$ROOT/results/final/T7" 2>/dev/null || true
mkdir -p "$ROOT/results/final/T7/raw"

first=1
rc_any=0
for seed in "${SEEDS[@]}"; do
  for mode in "${MODES[@]}"; do
    say "===== $mode / seed=$seed ====="
    reset=""
    if [ "$first" = 1 ]; then reset="--reset"; first=0; fi
    CLOUD_C1="$CLOUD_C1" CLOUD_C2="$CLOUD_C2" python3 scripts/run_t7.py --mode "$mode" --seed "$seed" $reset || rc_any=1
  done
done

say "aggregating -> results/final/T7/ (summary.csv, plots/, README.md)"
python3 scripts/run_t7.py --plot-only

say "done. See results/final/T7/README.md"
exit "$rc_any"
