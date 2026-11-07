#!/usr/bin/env bash
# =============================================================================
# T1.sh -- one-touch T1 delivery run across all three configurations.
#
#   Request: "Need a pediatric infusion pump at 2B-12, urgent."
#   Runs it end to end under C1, then C2, then C3, one at a time:
#     clean start  ->  bring the stack up  ->  submit the request  ->
#     wait for "Final Mission Status: COMPLETED" in the edge log  ->
#     save that run's logs  ->  full cleanup  ->  next configuration.
#
# Usage:
#   ./T1.sh                 # Gazebo (6 racecars), configs C1 C2 C3
#   ./T1.sh kinematic       # Gazebo-less kinematic simulator
#   T1_CONFIGS="C1 C3" ./T1.sh          # a subset, in that order
#   T1_TIMEOUT=1500 ./T1.sh gazebo      # longer per-config wait (seconds)
#
# Notes:
#   * All three run on the LOCAL cloud (NIMBUS_LOCAL_CLOUD=1): C2/C3 require it,
#     and it keeps the comparison on one identical stack.
#   * C1/C2 use the cloud LLM. For real Bedrock, export NIMBUS_LLM=bedrock (and
#     AWS creds) before running; otherwise the deterministic stand-in is used
#     and the mission still completes. C3 never calls a model.
#   * Results (per-config edge/cloud logs) are written under T1_results/.
# =============================================================================
set -uo pipefail
cd "$(dirname "$0")"
ROOT="$PWD"

SIM_MODE="${1:-gazebo}"                       # gazebo | kinematic | auto
CONFIGS=(${T1_CONFIGS:-C1 C2 C3})
TIMEOUT="${T1_TIMEOUT:-1200}"                 # seconds to wait for completion, per config
PORT="${PORT:-8000}"
CLOUD="http://127.0.0.1:${PORT}"
REQUEST_TEXT="Need a pediatric infusion pump at 2B-12, urgent."
LOGS="$ROOT/logs"
EAST_LOG="$LOGS/edge_east-wing.log"
WEST_LOG="$LOGS/edge_west-wing.log"
CLOUD_LOG="$LOGS/cloud.log"
RESULTS="$ROOT/T1_results"
DONE="Final Mission Status: COMPLETED"
TERMINAL_RE="Final Mission Status: (FAILED|REJECTED|CANCELLED|EXPIRED|SUPERSEDED)"

mkdir -p "$RESULTS"

# Always leave the machine clean, even on Ctrl-C or an error.
cleanup() { ./stop_all.sh >/dev/null 2>&1 || true; }
trap cleanup EXIT INT TERM

say() { printf '\n\033[1m[T1] %s\033[0m\n' "$*"; }

post_json() { curl -fsS -X POST "$CLOUD$1" -H 'Content-Type: application/json' -d "$2" 2>/dev/null; }

jget() { python3 -c "import sys,json;
try: print(json.load(sys.stdin).get('$1',''))
except Exception: print('')" 2>/dev/null; }

wait_cloud_up() {
  for _ in $(seq 1 60); do
    curl -fs "$CLOUD/fleet" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

submit_request() {
  local mode="$1" mid="t1-${mode}-$(date +%s)-$$"
  if [ "$mode" = C3 ]; then
    # C3 ingress is the structured form (no language model). 2B-12 is bed 12
    # in ward 2B; the form works at ward granularity, so the destination is 2B.
    post_json /requests \
      "{\"form\":{\"item\":\"infusion_pump_paediatric\",\"destination\":\"2B\",\"priority\":\"urgent\",\"deadline_minutes\":12},\"message_id\":\"$mid\"}"
  else
    # C1/C2 ingress is plain language through the cloud LLM.
    local resp status tid
    resp="$(post_json /requests "{\"text\":\"$REQUEST_TEXT\",\"message_id\":\"$mid\"}")"
    status="$(printf '%s' "$resp" | jget status)"
    tid="$(printf '%s' "$resp" | jget task_id)"
    printf '%s\n' "$resp"
    # If the cloud asks a clarifying question, answer with its first reading so
    # the run proceeds unattended.
    if [ "$status" = "needs_clarification" ] && [ -n "$tid" ]; then
      local reading
      reading="$(printf '%s' "$resp" | python3 -c "import sys,json;q=json.load(sys.stdin)['question'];print(q['possible_readings'][0])" 2>/dev/null)"
      [ -n "$reading" ] && post_json "/requests/$tid/answer" "{\"text\":\"$reading\",\"message_id\":\"$mid-ans\"}"
    fi
  fi
}

wait_for_completion() {
  # run_all.sh freshly overwrites the edge logs when it starts, and no mission
  # has run before we submit, so a plain grep cannot match a stale line.
  local deadline=$(( $(date +%s) + TIMEOUT ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if grep -qE "$DONE" "$EAST_LOG" "$WEST_LOG" 2>/dev/null; then
      return 0
    fi
    if grep -qE "$TERMINAL_RE" "$EAST_LOG" "$WEST_LOG" 2>/dev/null; then
      return 2   # mission ended in a non-completed terminal state
    fi
    sleep 3
  done
  return 1       # timed out
}

run_config() {
  local mode="$1"
  say "===== Configuration $mode : starting clean ====="
  ./stop_all.sh >/dev/null 2>&1 || true

  say "$mode: bringing the stack up ($SIM_MODE, local cloud)"
  NIMBUS_LOCAL_CLOUD=1 ./run_all.sh "$SIM_MODE" --system-mode "$mode" || {
    say "$mode: run_all.sh failed to start the stack"; return 1; }

  if ! wait_cloud_up; then
    say "$mode: local cloud never came up on $CLOUD"; return 1
  fi
  # Report what is actually running (mode + LLM backend).
  curl -fs "$CLOUD/fleet" 2>/dev/null | python3 -c "import sys,json;d=json.load(sys.stdin);print(f\"[T1] running mode={d.get('mode')} llm={d.get('llm')} pack={d.get('pack')}\")" 2>/dev/null || true
  sleep 3   # let the edge supervisors finish their first lease poll

  say "$mode: submitting -> \"$REQUEST_TEXT\""
  submit_request "$mode"

  say "$mode: waiting up to ${TIMEOUT}s for \"$DONE\" ..."
  local rc; wait_for_completion; rc=$?

  # Preserve this run's evidence before the next config overwrites the logs.
  local outdir="$RESULTS/$mode"; mkdir -p "$outdir"
  cp -f "$EAST_LOG" "$outdir/edge_east-wing.log" 2>/dev/null || true
  cp -f "$WEST_LOG" "$outdir/edge_west-wing.log" 2>/dev/null || true
  cp -f "$CLOUD_LOG" "$outdir/cloud.log" 2>/dev/null || true

  case "$rc" in
    0) say "$mode: COMPLETED  (logs saved in ${outdir#$ROOT/})"; RESULT[$mode]="COMPLETED" ;;
    2) local line; line="$(grep -hE "Final Mission Status:" "$EAST_LOG" "$WEST_LOG" 2>/dev/null | tail -1)"
       say "$mode: ended NOT completed -> ${line:-unknown}  (logs in ${outdir#$ROOT/})"; RESULT[$mode]="${line:-NOT_COMPLETED}" ;;
    *) say "$mode: TIMED OUT after ${TIMEOUT}s  (logs in ${outdir#$ROOT/})"; RESULT[$mode]="TIMEOUT" ;;
  esac

  say "$mode: cleanup"
  ./stop_all.sh >/dev/null 2>&1 || true
  sleep 3   # let Gazebo/ROS fully release before the next start
  [ "$rc" = 0 ]
}

declare -A RESULT
overall=0
for mode in "${CONFIGS[@]}"; do
  case "$mode" in C1|C2|C3) ;; *) say "skipping unknown config '$mode'"; continue ;; esac
  run_config "$mode" || overall=1
done

say "================= T1 SUMMARY ================="
for mode in "${CONFIGS[@]}"; do
  printf '   %-3s : %s\n' "$mode" "${RESULT[$mode]:-SKIPPED}"
done
echo "   Per-config logs: ${RESULTS#$ROOT/}/<C1|C2|C3>/"
exit "$overall"
