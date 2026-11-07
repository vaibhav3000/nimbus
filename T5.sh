#!/usr/bin/env bash
# =============================================================================
# T5.sh -- one-touch T5: gate closed -> cross-wing hand-over, on the REAL cloud.
#
#   Mission: deliver the paediatric infusion pump to 2B-12. The pickup is the shared
#   store, reachable only via the two south corners; the east wing's corner
#   (east_south_hall_corner) is reported CLOSED by the edge, so the east wing can't
#   reach the pickup and the job must cross wings (west collects; the pump reaches 2B
#   via a hand-off between the wings).
#
#   Cloud (project rule -- C1/C2 ALWAYS use the deployed endpoints):
#     C1 -> deployed qrgdidp9te (real Bedrock)
#     C2 -> deployed xkczmnuqla
#     C3 -> local classical cloud on the real hospital_sim6 pack
#   Edge: scripts/edge_sim.py (one per wing, kinematic --pace); east reports the
#   closed corner via the real edge closure contract. REAL run: creates real tasks
#   on the deployed cloud and shows in the operator GUI. Uses the REAL map (no pack
#   or map change on the cloud) -- the scenario is designed around the real cloud.
#
# Usage:
#   ./T5.sh                        # C1 C2 C3, one seed
#   T5_MODES="C1 C2" T5_SEEDS="1 2" ./T5.sh
#   CLOUD_C1=... CLOUD_C2=... ./T5.sh      # override endpoints
# =============================================================================
set -uo pipefail
cd "$(dirname "$0")"
ROOT="$PWD"

for arg in "$@"; do
  case "$arg" in
    *) echo "usage: $0   (knobs via T5_MODES/T5_SEEDS/CLOUD_C1/CLOUD_C2)" >&2; exit 2 ;;
  esac
done

MODES=(${T5_MODES:-C1 C2 C3})
SEEDS=(${T5_SEEDS:-1})
CLOUD_C1="${CLOUD_C1:-https://qrgdidp9te.execute-api.ap-south-1.amazonaws.com}"
CLOUD_C2="${CLOUD_C2:-https://xkczmnuqla.execute-api.ap-south-1.amazonaws.com}"

say() { printf '\n\033[1m[T5] %s\033[0m\n' "$*"; }

say "sweep: modes=[${MODES[*]}] seeds=[${SEEDS[*]}] (C1/C2 deployed, C3 local; real edge_sim)"

# Reset FR-19 long-term memory (learned habits/closures/restrictions) on BOTH deployed
# clouds up front, so persistent state can't bias allocation/reaction between runs and
# the cross-wing hand-over is reproducible. (run_t5.py also resets per trial.)
say "resetting long-term memory on deployed C1 and C2"
for url in "$CLOUD_C1" "$CLOUD_C2"; do
  if curl -fsS -m 25 -X POST "${url%/}/reset-memory" -H 'Content-Type: application/json' -d '{}' >/dev/null 2>&1; then
    echo "  reset ok   : ${url%/}"
  else
    echo "  reset FAILED: ${url%/}  (continuing; run_t5.py resets again per trial)"
  fi
done

say "clearing results/final/T5/ for a clean run"
rm -rf "$ROOT/results/final/T5" 2>/dev/null || true
mkdir -p "$ROOT/results/final/T5/raw"

first=1
rc_any=0
for seed in "${SEEDS[@]}"; do
  for mode in "${MODES[@]}"; do
    say "===== $mode / seed=$seed ====="
    reset=""
    if [ "$first" = 1 ]; then reset="--reset"; first=0; fi
    python3 scripts/run_t5.py --mode "$mode" --seed "$seed" $reset || rc_any=1
  done
done

say "aggregating -> results/final/T5/ (summary.csv, plots/, README.md)"
python3 scripts/run_t5.py --plot-only

say "done. See results/final/T5/README.md"
exit "$rc_any"
