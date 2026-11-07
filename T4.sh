#!/usr/bin/env bash
# =============================================================================
# T4.sh -- one-touch T4: blocked corridor; detour inside the lease zone.
#
#   For each configuration (C1/C2/C3), runs one delivery through the REAL Nimbus
#   workflow (request -> allocation -> rule checker -> lease -> real edge
#   supervisor, kinematic mock robot), with a persistent corridor obstacle that
#   has a valid detour INSIDE the lease zone, while the edge<->cloud link is a BAD
#   link (degraded latency/jitter/loss) for the whole mission.
#
#   Expected contrast (the point of T4):
#     C1  edge SLM decides the detour LOCALLY -> bad link not on decision path -> FASTEST
#     C2  cloud-only relay round-trips to the cloud per blocked step           -> STALLS on the link
#     C3  local finite-state rules detour                                      -> also fast (classical)
#
#   The heavy lifting is in scripts/run_t4.py. Kinematic simulator throughout
#   (real supervisor in-process, no Gazebo). Like T2, T4 owns a LOCAL cloud: the
#   bad link is injected with an in-process FaultProxy, which a managed AWS endpoint
#   cannot be. (T1/T8 have no impairment and use the deployed C1/C2 endpoints.)
#
# Usage:
#   ./T4.sh                              # C1 C2 C3 on the 'severe' bad link
#   T4_PROFILES="healthy severe" ./T4.sh # add a healthy baseline (shows C2's latency jump)
#   T4_PROFILES="800:200:0.1" ./T4.sh    # custom link: latency_ms:jitter_ms:loss_rate
#   T4_MODES="C1 C2" T4_SEEDS="1 2" ./T4.sh
#
# Profiles: healthy | mild_wan | shielded_room | severe | <lat:jitter:loss>
# =============================================================================
set -uo pipefail
cd "$(dirname "$0")"
ROOT="$PWD"

for arg in "$@"; do
  case "$arg" in
    *) echo "usage: $0   (knobs via T4_MODES/T4_PROFILES/T4_SEEDS)" >&2; exit 2 ;;
  esac
done

MODES=(${T4_MODES:-C1 C2 C3})
PROFILES=(${T4_PROFILES:-severe})
SEEDS=(${T4_SEEDS:-1})

say() { printf '\n\033[1m[T4] %s\033[0m\n' "$*"; }

say "sweep: modes=[${MODES[*]}] profiles=[${PROFILES[*]}] seeds=[${SEEDS[*]}] (kinematic, real supervisor)"
say "clearing results/final/T4/ for a clean run"
rm -rf "$ROOT/results/final/T4" 2>/dev/null || true
mkdir -p "$ROOT/results/final/T4/raw"

first=1
rc_any=0
for seed in "${SEEDS[@]}"; do
  for mode in "${MODES[@]}"; do
    for profile in "${PROFILES[@]}"; do
      say "===== $mode / $profile / seed=$seed ====="
      reset=""
      if [ "$first" = 1 ]; then reset="--reset"; first=0; fi
      python3 scripts/run_t4.py --mode "$mode" --profile "$profile" --seed "$seed" $reset || rc_any=1
    done
  done
done

say "aggregating -> results/final/T4/ (summary.csv, plots/, README.md)"
python3 scripts/run_t4.py --plot-only

say "done. See results/final/T4/README.md"
exit "$rc_any"
