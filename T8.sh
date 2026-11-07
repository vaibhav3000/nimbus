#!/usr/bin/env bash
# =============================================================================
# T8.sh -- one-touch T8 "3 simultaneous requests, 2 robots" across C1/C2/C3.
#
#   Three requests hit the SAME east ward (2B) at once, while only TWO robots are
#   available (one east, one west):
#       1. routine : wheelchair          -> 2B
#       2. routine : blanket             -> 2B
#       3. urgent  : paediatric pump     -> 2B-12
#   The single east robot can serve one at a time, so two queue. How well the
#   urgent one is served under contention is the whole point:
#     * C1 / C2 (LLM orchestration on the CLOUD) prioritise the urgent request.
#     * C3 (no LLM) follows its classical scheduler.
#
#   Cloud endpoint is per-config, like T1:
#     C1 -> deployed AWS cloud   (requests show in that cloud's operator GUI)
#     C2 -> deployed AWS cloud   (its own dedicated endpoint)
#     C3 -> local cloud
#   On a remote cloud, run_all.sh runs the edge with the deployed-two-cluster
#   robot profile (robot_1 east + robot_2 west) -- i.e. exactly the 2 robots the
#   scenario needs. Kinematic simulator throughout (like T2), no Gazebo required.
#
# Usage:
#   ./T8.sh                       # kinematic, configs C1 C2 C3
#   ./T8.sh gazebo                # full Gazebo instead of kinematic
#   T8_CONFIGS="C1 C3" ./T8.sh    # a subset, in that order
#   T8_TIMEOUT=1500 ./T8.sh       # longer per-config wait (seconds)
#
# Endpoints (override via env):
#   CLOUD_C1, CLOUD_C2 (deployed), CLOUD_C3 (local). NIMBUS_LLM=bedrock (+creds)
#   for real Bedrock on C1/C2; otherwise the deterministic stand-in is used.
#
# Results (per-config edge/cloud logs) are written under T8_results/.
# =============================================================================
set -uo pipefail
cd "$(dirname "$0")"
ROOT="$PWD"

SIM_MODE="${1:-kinematic}"                    # kinematic (default, like T2) | gazebo | auto
CONFIGS=(${T8_CONFIGS:-C1 C2 C3})
TIMEOUT="${T8_TIMEOUT:-1200}"                 # seconds to wait for all 3 to finish, per config
CLOUD_C1="${CLOUD_C1:-https://qrgdidp9te.execute-api.ap-south-1.amazonaws.com}"
CLOUD_C2="${CLOUD_C2:-https://xkczmnuqla.execute-api.ap-south-1.amazonaws.com}"
CLOUD_C3="${CLOUD_C3:-127.0.0.1:8000}"
CLOUD=""   # set per-config in run_config()

LOGS="$ROOT/logs"
EAST_LOG="$LOGS/edge_east-wing.log"
WEST_LOG="$LOGS/edge_west-wing.log"
CLOUD_LOG="$LOGS/cloud.log"
RESULTS="$ROOT/T8_results"
DONE="Final Mission Status: COMPLETED"
TERMINAL_RE="Final Mission Status: (FAILED|REJECTED|CANCELLED|EXPIRED|SUPERSEDED)"
N_REQUESTS=3

# The three simultaneous requests. text|<free text>  OR  form|<item>|<dest>|<priority>|<deadline_min>
REQ_TEXT=(
  "Deliver a wheelchair to 2B, routine."
  "Bring a blanket to ward 2B, routine."
  "Need a pediatric infusion pump at 2B-12, urgent."
)
REQ_FORM=(
  "wheelchair|2B|routine|60"
  "blanket|2B|routine|60"
  "infusion_pump_paediatric|2B|urgent|12"
)

mkdir -p "$RESULTS"
cleanup() { ./stop_all.sh >/dev/null 2>&1 || true; }
trap cleanup EXIT INT TERM

say() { printf '\n\033[1m[T8] %s\033[0m\n' "$*"; }
normalize_url() { local u="$1"; case "$u" in http://*|https://*) ;; *) u="http://$u" ;; esac; printf '%s' "${u%/}"; }
is_local_url()  { case "$1" in *127.0.0.1*|*localhost*) return 0 ;; *) return 1 ;; esac; }
post_json() { curl -fsS -m 30 -X POST "$CLOUD$1" -H 'Content-Type: application/json' -d "$2" 2>/dev/null; }
jget() { python3 -c "import sys,json
try: print(json.load(sys.stdin).get('$1',''))
except Exception: print('')" 2>/dev/null; }

wait_cloud_up() {
  for _ in $(seq 1 60); do
    curl -fs -m 5 "$CLOUD/fleet" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

submit_one() {
  local mode="$1" idx="$2"
  local mid="t8-${mode}-$(date +%s)-$$-$idx"
  if [ "$mode" = C3 ]; then
    IFS='|' read -r item dest prio dl <<<"${REQ_FORM[$idx]}"
    post_json /requests \
      "{\"form\":{\"item\":\"$item\",\"destination\":\"$dest\",\"priority\":\"$prio\",\"deadline_minutes\":$dl},\"message_id\":\"$mid\"}" >/dev/null
  else
    local resp status tid reading
    resp="$(post_json /requests "{\"text\":\"${REQ_TEXT[$idx]}\",\"message_id\":\"$mid\"}")"
    status="$(printf '%s' "$resp" | jget status)"
    tid="$(printf '%s' "$resp" | jget task_id)"
    if [ "$status" = "needs_clarification" ] && [ -n "$tid" ]; then
      reading="$(printf '%s' "$resp" | python3 -c "import sys,json;q=json.load(sys.stdin)['question'];print(q['possible_readings'][0])" 2>/dev/null)"
      [ -n "$reading" ] && post_json "/requests/$tid/answer" "{\"text\":\"$reading\",\"message_id\":\"$mid-ans\"}" >/dev/null
    fi
  fi
}

submit_all() {
  local mode="$1" i
  for i in $(seq 0 $((N_REQUESTS-1))); do
    submit_one "$mode" "$i"
  done
}

# sum "COMPLETED" lines across both edge logs (no bc: -h strips filenames, wc counts)
count_done() { grep -hE -- "$DONE" "$EAST_LOG" "$WEST_LOG" 2>/dev/null | wc -l | tr -d ' '; }

wait_for_all() {
  local deadline=$(( $(date +%s) + TIMEOUT )) done_n
  while [ "$(date +%s)" -lt "$deadline" ]; do
    done_n="$(count_done)"
    [ "${done_n:-0}" -ge "$N_REQUESTS" ] && return 0
    sleep 3
  done
  return 1
}

run_config() {
  local mode="$1"
  case "$mode" in
    C1) CLOUD="$(normalize_url "$CLOUD_C1")" ;;
    C2) CLOUD="$(normalize_url "$CLOUD_C2")" ;;
    C3) CLOUD="$(normalize_url "$CLOUD_C3")" ;;
  esac
  say "===== Configuration $mode : starting clean (cloud $CLOUD) ====="
  ./stop_all.sh >/dev/null 2>&1 || true

  if is_local_url "$CLOUD"; then
    say "$mode: bringing the stack up ($SIM_MODE, local cloud $CLOUD)"
    NIMBUS_LOCAL_CLOUD=1 ./run_all.sh "$SIM_MODE" --system-mode "$mode" || {
      say "$mode: run_all.sh failed to start the stack"; return 1; }
  else
    say "$mode: bringing the stack up ($SIM_MODE, remote cloud $CLOUD, 2-robot profile)"
    NIMBUS_CLOUD_URL="$CLOUD" ./run_all.sh "$SIM_MODE" --system-mode "$mode" || {
      say "$mode: run_all.sh failed to start the stack"; return 1; }
  fi

  if ! wait_cloud_up; then
    say "$mode: cloud never came up on $CLOUD"; return 1
  fi
  curl -fs -m 5 "$CLOUD/fleet" 2>/dev/null | python3 -c "import sys,json;d=json.load(sys.stdin);print(f\"[T8] running mode={d.get('mode')} llm={d.get('llm')} pack={d.get('pack')}\")" 2>/dev/null || true
  sleep 3   # let the edge supervisors finish their first lease poll

  say "$mode: submitting $N_REQUESTS simultaneous requests to ward 2B (2 routine + 1 urgent)"
  submit_all "$mode"

  say "$mode: waiting up to ${TIMEOUT}s for all $N_REQUESTS to reach \"$DONE\" ..."
  local rc; wait_for_all; rc=$?

  local outdir="$RESULTS/$mode"; mkdir -p "$outdir"
  cp -f "$EAST_LOG" "$outdir/edge_east-wing.log" 2>/dev/null || true
  cp -f "$WEST_LOG" "$outdir/edge_west-wing.log" 2>/dev/null || true
  cp -f "$CLOUD_LOG" "$outdir/cloud.log" 2>/dev/null || true
  local done_n; done_n="$(count_done)"; done_n="${done_n:-0}"

  if [ "$rc" = 0 ]; then
    say "$mode: all $N_REQUESTS COMPLETED  (logs in ${outdir#$ROOT/})"; RESULT[$mode]="COMPLETED ${done_n}/${N_REQUESTS}"
  else
    say "$mode: only ${done_n}/${N_REQUESTS} completed within ${TIMEOUT}s  (logs in ${outdir#$ROOT/})"
    RESULT[$mode]="PARTIAL ${done_n}/${N_REQUESTS}"
  fi

  say "$mode: cleanup"
  ./stop_all.sh >/dev/null 2>&1 || true
  sleep 3
  [ "$rc" = 0 ]
}

# Reset FR-19 long-term memory (learned habits/closures/restrictions) on BOTH deployed
# clouds first, so persistent state can't bias allocation/scheduling between runs.
say "resetting long-term memory on deployed C1 and C2"
for url in "$CLOUD_C1" "$CLOUD_C2"; do
  u="$(normalize_url "$url")"
  if curl -fsS -m 25 -X POST "$u/reset-memory" -H 'Content-Type: application/json' -d '{}' >/dev/null 2>&1; then
    printf '    reset ok   : %s\n' "$u"
  else
    printf '    reset FAILED: %s (continuing)\n' "$u"
  fi
done

declare -A RESULT
overall=0
for mode in "${CONFIGS[@]}"; do
  case "$mode" in C1|C2|C3) ;; *) say "skipping unknown config '$mode'"; continue ;; esac
  run_config "$mode" || overall=1
done

say "================= T8 SUMMARY ================="
for mode in "${CONFIGS[@]}"; do
  printf '   %-3s : %s\n' "$mode" "${RESULT[$mode]:-SKIPPED}"
done
echo "   Per-config logs: ${RESULTS#$ROOT/}/<C1|C2|C3>/"
exit "$overall"
