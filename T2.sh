#!/usr/bin/env bash
# =============================================================================
# T2.sh -- one-touch T2 experiment: urgent delivery under a sustained WAN outage.
#
#   For each configuration (C1/C2/C3), runs one urgent delivery through the REAL
#   Nimbus workflow (request -> allocation -> rule checker -> lease -> real edge
#   supervisor), with the cloud<->edge WAN cut the instant the lease is granted
#   and held to the end of the mission, and a corridor obstacle forcing a tactical
#   decision mid-route. It then writes the standard ResultDir tree under
#   results/final/T2/.
#
#   Expected contrast (the whole point of T2):
#     C1  edge SLM detours locally, no cloud needed          -> DELIVERED
#     C2  cloud-only relay can't reach the cloud to decide    -> HALTS + escalates
#         (escalation stuck in the offline outbox)            -> delivery FAILS
#     C3  local finite-state rules take a map-valid detour    -> DELIVERED
#
#   The heavy lifting is in scripts/run_t2.py (see its header for the method and
#   the honest account of what "outage" and the measurements mean on a host with
#   no tc/ping). This is OUTAGE-ONLY by design: a healthy run produces no config
#   contrast (everyone just completes), so it is not run.
#
#   WHY A LOCAL CLOUD, NOT THE DEPLOYED ENDPOINT (unlike T1/T8):
#   T2 is fundamentally a NETWORK-DISRUPTION SIMULATION -- its purpose is to cut and
#   restore the cloud<->edge link on a controlled schedule, which cannot be applied to
#   a managed AWS endpoint from this host. Its mechanism is a precisely-timed WAN cut
#   -- the outage is created by
#   SIGSTOP-ing the cloud PROCESS the instant the lease is granted and resuming it
#   at mission end. A managed AWS deployment (API Gateway + Fargate) cannot be
#   paused and its cloud<->edge link cannot be severed at a chosen instant, so T2
#   must own a local cloud. It also needs a short-lease compressed-timing pack so
#   C2's offline stall terminates in seconds, which the fixed deployed cloud will
#   not provide. See the "Why T2 uses a local cloud" section of the generated
#   results/final/T2/README.md and env.json for the full reasoning. T1 and T8 have
#   no outage, so they DO use the deployed C1/C2 endpoints.
#
# Usage:
#   ./T2.sh                         # C1 C2 C3, one seed
#   T2_MODES="C1 C2" T2_SEEDS="1 2" ./T2.sh
#
# Env knobs: T2_MODES, T2_SEEDS, T2_IFACE (apply real netem there if tc exists),
# NIMBUS_LLM=bedrock (+creds) for real Bedrock on C1/C2, NIMBUS_PACK to override
# the compressed-timing pack (default packs/hospital_sim6_t2).
# =============================================================================
set -uo pipefail
cd "$(dirname "$0")"
ROOT="$PWD"

for arg in "$@"; do
  case "$arg" in
    *) echo "usage: $0            (outage-only; knobs via T2_MODES/T2_SEEDS/T2_IFACE)" >&2; exit 2 ;;
  esac
done

MODES=(${T2_MODES:-C1 C2 C3})
SEEDS=(${T2_SEEDS:-1})

say() { printf '\n\033[1m[T2] %s\033[0m\n' "$*"; }

say "sweep: modes=[${MODES[*]}] seeds=[${SEEDS[*]}] profile=[outage] (real edge supervisor)"
say "clearing results/final/T2/ for a clean run"
rm -rf "$ROOT/results/final/T2" 2>/dev/null || true
mkdir -p "$ROOT/results/final/T2/raw"

first=1
rc_any=0
for seed in "${SEEDS[@]}"; do
  for mode in "${MODES[@]}"; do
    say "===== $mode / outage / seed=$seed ====="
    reset=""
    if [ "$first" = 1 ]; then reset="--reset"; first=0; fi
    python3 scripts/run_t2.py --mode "$mode" --seed "$seed" $reset \
      ${T2_IFACE:+--iface "$T2_IFACE"} || rc_any=1
  done
done

say "aggregating -> results/final/T2/ (summary.csv, plots/, README.md)"
python3 scripts/run_t2.py --plot-only

say "done. See results/final/T2/README.md"
exit "$rc_any"
