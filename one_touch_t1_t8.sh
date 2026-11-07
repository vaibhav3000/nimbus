#!/usr/bin/env bash
# =============================================================================
# one_touch_t1_t8.sh -- run the whole experiment suite T1..T8 in one shot.
#
#   For each test it:
#     1. runs T<n>.sh (kinematic throughout; C1/C2 on the deployed clouds, C3 local),
#     2. ARCHIVES that test's outputs into a TIMESTAMPED run folder so previous runs
#        are never overwritten,
#     3. RESETS FR-19 long-term memory on BOTH deployed clouds AFTER the run (and once
#        before the suite) so each test starts from a clean cloud.
#
#   Results tree (nothing is overwritten across suite runs):
#     runs/<UTC-timestamp>/
#       T1/ ... T8/           <- a copy of each test's ResultDir / logs
#       T<n>.console.log      <- full console output of each test
#       SUMMARY.txt           <- per-test exit status + where its evidence went
#
# Usage:
#   ./one_touch_t1_t8.sh                 # T1..T8
#   TESTS="T2 T4 T7" ./one_touch_t1_t8.sh   # a subset, in that order
#   CLOUD_C1=... CLOUD_C2=... ./one_touch_t1_t8.sh
#
# Note: T1/T3/T8 bring up the full stack via run_all.sh in KINEMATIC mode (no Gazebo);
# T2/T4/T5/T6/T7 drive the kinematic edge directly. The suite is sequential; the real
# deployed cloud is slow, so a full pass takes a while.
# =============================================================================
set -uo pipefail
cd "$(dirname "$0")"
ROOT="$PWD"

TS="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_DIR="$ROOT/runs/$TS"
mkdir -p "$RUN_DIR"

CLOUD_C1="${CLOUD_C1:-https://qrgdidp9te.execute-api.ap-south-1.amazonaws.com}"
CLOUD_C2="${CLOUD_C2:-https://xkczmnuqla.execute-api.ap-south-1.amazonaws.com}"
export CLOUD_C1 CLOUD_C2
TESTS=(${TESTS:-T1 T2 T3 T4 T5 T6 T7 T8})

SUMMARY="$RUN_DIR/SUMMARY.txt"

say() { printf '\n\033[1m[SUITE] %s\033[0m\n' "$*"; }

# Each test's output location(s), and whether it takes a "kinematic" sim arg.
outdir_for() {
  case "$1" in
    T1) echo "T1_results" ;;
    T2) echo "results/final/T2" ;;
    T3) echo "T3_results" ;;
    T4) echo "results/final/T4" ;;
    T5) echo "results/final/T5" ;;
    T6) echo "T6_results" ;;
    T7) echo "results/final/T7" ;;
    T8) echo "T8_results" ;;
    *)  echo "" ;;
  esac
}
simarg_for() {   # T1/T3/T8 accept a sim mode as $1; pass kinematic. Others take none.
  case "$1" in T1|T3|T8) echo "kinematic" ;; *) echo "" ;; esac
}

reset_memory() {
  for url in "$CLOUD_C1" "$CLOUD_C2"; do
    if curl -fsS -m 25 -X POST "${url%/}/reset-memory" -H 'Content-Type: application/json' -d '{}' >/dev/null 2>&1; then
      echo "    reset ok   : ${url%/}"
    else
      echo "    reset FAILED: ${url%/}"
    fi
  done
}

{
  echo "Nimbus experiment suite T1..T8"
  echo "run id      : $TS"
  echo "tests       : ${TESTS[*]}"
  echo "cloud C1/C2 : $CLOUD_C1 | $CLOUD_C2"
  echo "started_utc : $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo
} | tee "$SUMMARY"

say "initial memory reset on deployed C1 and C2 (clean start)"
reset_memory | tee -a "$SUMMARY"

overall=0
for t in "${TESTS[@]}"; do
  script="./$t.sh"
  if [ ! -f "$script" ]; then
    say "SKIP $t (no $script)"; printf '%-4s : SKIPPED (no script)\n' "$t" >>"$SUMMARY"; continue
  fi

  say "===== running $t ====="
  ./stop_all.sh >/dev/null 2>&1 || true          # make sure nothing lingers from the previous test
  arg="$(simarg_for "$t")"
  # shellcheck disable=SC2086
  bash "$script" $arg >"$RUN_DIR/$t.console.log" 2>&1; rc=$?
  ./stop_all.sh >/dev/null 2>&1 || true

  # archive this test's evidence into the timestamped run folder (copy, don't move)
  src="$(outdir_for "$t")"
  archived="(none)"
  if [ -n "$src" ] && [ -d "$ROOT/$src" ]; then
    mkdir -p "$RUN_DIR/$t"
    cp -a "$ROOT/$src/." "$RUN_DIR/$t/" 2>/dev/null || true
    archived="runs/$TS/$t/"
  fi

  if [ "$rc" -eq 0 ]; then status="OK"; else status="rc=$rc"; overall=1; fi
  printf '%-4s : %-8s evidence=%s  console=runs/%s/%s.console.log\n' "$t" "$status" "$archived" "$TS" "$t" >>"$SUMMARY"
  say "$t done ($status); evidence -> $archived"

  say "post-$t memory reset on deployed C1 and C2"
  reset_memory | tee -a "$SUMMARY"
done

{
  echo
  echo "finished_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "overall     : $([ "$overall" -eq 0 ] && echo ALL-OK || echo SOME-FAILED)"
} | tee -a "$SUMMARY"

say "suite complete. Results archived under runs/$TS/  (SUMMARY.txt there)"
exit "$overall"
