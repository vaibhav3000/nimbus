#!/usr/bin/env bash
# =============================================================================
# T3.sh -- one-touch T3 "vague clinical request" run across C1, C2, C3.
#
#   Request (vague): "a pump to 2B"   (2B is a paediatric ward, so "a pump"
#   is ambiguous: paediatric vs standard infusion pump.)
#
#   Expected outcome (PRD Table 3, "C1, C2 >> C3"):
#     * C1 / C2 -- the cloud LLM recognises the ambiguity, ASKS a follow-up
#       ("do you need a paediatric pump?"), we answer, and the delivery
#       completes.  -> PASS (handled via clarification).
#     * C3 -- the no-LLM baseline has only the structured form, no
#       conversational follow-up.  The naive choice ("a pump" = the generic
#       infusion pump) to a paediatric ward is flagged for clarification that
#       C3 cannot resolve on its own, so no lease is issued and nothing is
#       delivered.  -> FAIL (expected): the vague request cannot be handled.
#
#   Per configuration: clean start -> bring the stack up -> submit ->
#     C1/C2: answer the follow-up, wait for "Final Mission Status: COMPLETED";
#     C3:    confirm the request dead-ends at clarification (no lease).
#   Then save logs, full cleanup, next configuration.
#
# Usage:
#   ./T3.sh                 # Gazebo, configs C1 C2 C3
#   ./T3.sh kinematic       # Gazebo-less simulator
#   T3_CONFIGS="C1 C3" ./T3.sh
#   T3_TIMEOUT=1500 ./T3.sh gazebo
#
# Notes:
#   * Cloud endpoint is per-config (CLOUD_C1/CLOUD_C2/CLOUD_C3). C1/C2 target
#     their deployed AWS clouds; C3 a local cloud. A local URL is launched with
#     NIMBUS_LOCAL_CLOUD=1, a remote URL with NIMBUS_CLOUD_URL.
#   * For real Bedrock on a local cloud, export NIMBUS_LLM=bedrock (+ creds).
#   * A cloud that has already served many "... to 2B" requests LEARNS the
#     habit (FR-19) and stops asking -- it resolves "a pump to 2B" to the
#     paediatric pump on its own. So before each submit this script resets the
#     cloud's learned habits/closures (POST /reset-memory) to make the request
#     genuinely ambiguous again. Set T3_RESET_MEMORY=0 to skip that (note: on a
#     trained cloud T3 then cannot show the clarification). The reset clears
#     SHARED learned state on a deployed cloud.
#   * Results are written under T3_results/.
# =============================================================================
set -uo pipefail
cd "$(dirname "$0")"
ROOT="$PWD"

SIM_MODE="${1:-gazebo}"
CONFIGS=(${T3_CONFIGS:-C1 C2 C3})
TIMEOUT="${T3_TIMEOUT:-1200}"
PORT="${PORT:-8000}"
RESET_MEMORY="${T3_RESET_MEMORY:-1}"   # clear learned habits so the request is ambiguous
CLOUD_C1="${CLOUD_C1:-https://qrgdidp9te.execute-api.ap-south-1.amazonaws.com}"
CLOUD_C2="${CLOUD_C2:-https://xkczmnuqla.execute-api.ap-south-1.amazonaws.com}"
CLOUD_C3="${CLOUD_C3:-127.0.0.1:8000}"
CLOUD=""
REQUEST_TEXT="a pump to 2B"
CLARIFY_ANSWER="Yes, a paediatric pump."
LOGS="$ROOT/logs"
EAST_LOG="$LOGS/edge_east-wing.log"
WEST_LOG="$LOGS/edge_west-wing.log"
CLOUD_LOG="$LOGS/cloud.log"
RESULTS="$ROOT/T3_results"
DONE="Final Mission Status: COMPLETED"
TERMINAL_RE="Final Mission Status: (FAILED|REJECTED|CANCELLED|EXPIRED|SUPERSEDED)"

mkdir -p "$RESULTS"

cleanup() { ./stop_all.sh >/dev/null 2>&1 || true; }
trap cleanup EXIT INT TERM

say() { printf '\n\033[1m[T3] %s\033[0m\n' "$*"; }

normalize_url() { local u="$1"; case "$u" in http://*|https://*) ;; *) u="http://$u" ;; esac; printf '%s' "${u%/}"; }
is_local_url()  { case "$1" in *127.0.0.1*|*localhost*) return 0 ;; *) return 1 ;; esac; }

post_json() { curl -fsS -X POST "$CLOUD$1" -H 'Content-Type: application/json' -d "$2" 2>/dev/null; }
jget() { python3 -c "import sys,json
try: print(json.load(sys.stdin).get('$1',''))
except Exception: print('')" 2>/dev/null; }

reset_memory() {
  # A cloud that has learned the 2B->paediatric habit will not ask; clear it so
  # the vague request is genuinely ambiguous again.
  [ "$RESET_MEMORY" = 1 ] || return 0
  say "$1: resetting cloud memory/habits (POST /reset-memory) so the request is ambiguous"
  if curl -fsS --max-time 20 -X POST "$CLOUD/reset-memory" -H 'Content-Type: application/json' -d '{}' >/dev/null 2>&1; then
    printf '    memory reset ok\n'
  else
    printf '    (memory reset call failed/unsupported; continuing -- C1/C2 may not clarify if already trained)\n'
  fi
}

wait_cloud_up() {
  for _ in $(seq 1 60); do
    curl -fs "$CLOUD/fleet" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

wait_for_completion() {
  local deadline=$(( $(date +%s) + TIMEOUT ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    grep -qE "$DONE" "$EAST_LOG" "$WEST_LOG" 2>/dev/null && return 0
    grep -qE "$TERMINAL_RE" "$EAST_LOG" "$WEST_LOG" 2>/dev/null && return 2
    sleep 3
  done
  return 1
}

# --- the T3 logic for one configuration ------------------------------------------------------
# C1/C2 take the vague request in plain language and must clarify; C3 can only
# take the structured form and cannot clarify, so it dead-ends.
run_llm_config() {   # C1 or C2
  local mode="$1" mid="t3-${mode}-$(date +%s)-$$" resp status tid
  say "$mode: submitting vague request -> \"$REQUEST_TEXT\""
  resp="$(post_json /requests "{\"text\":\"$REQUEST_TEXT\",\"message_id\":\"$mid\"}")"
  printf '    cloud reply: %s\n' "$resp"
  status="$(printf '%s' "$resp" | jget status)"
  tid="$(printf '%s' "$resp" | jget task_id)"
  if [ "$status" = "needs_clarification" ] && [ -n "$tid" ]; then
    local q; q="$(printf '%s' "$resp" | python3 -c "import sys,json;print(json.load(sys.stdin)['question']['question'])" 2>/dev/null)"
    say "$mode: cloud asked -> \"$q\"  (answering: \"$CLARIFY_ANSWER\")"
    post_json "/requests/$tid/answer" "{\"text\":\"$CLARIFY_ANSWER\",\"message_id\":\"$mid-ans\"}" >/dev/null
    CLARIFIED="yes"
  else
    say "$mode: no clarification was asked (status=$status) -- unexpected for T3"
    CLARIFIED="no"
  fi
  say "$mode: waiting up to ${TIMEOUT}s for \"$DONE\" ..."
  wait_for_completion; return $?
}

run_c3_config() {
  # C3 ingress is the structured form -- there is no free-text box. "a pump"
  # is the generic infusion pump; sent to paediatric ward 2B it is flagged for
  # a clarification the no-LLM baseline cannot resolve, so no lease is issued.
  local mid="t3-C3-$(date +%s)-$$" resp status
  say "C3: submitting the structured form (generic pump -> 2B; no free text in C3)"
  resp="$(post_json /requests "{\"form\":{\"item\":\"infusion_pump\",\"destination\":\"2B\",\"priority\":\"routine\",\"deadline_minutes\":60},\"message_id\":\"$mid\"}")"
  printf '    cloud reply: %s\n' "$resp"
  status="$(printf '%s' "$resp" | jget status)"
  if [ "$status" = "leased" ] || [ "$status" = "completed" ]; then
    say "C3: UNEXPECTED -- C3 leased the request without clarifying (status=$status)"
    return 3   # C3 did not fail; surprising for T3
  fi
  say "C3: request dead-ended at '$status' -- the structured, no-LLM baseline cannot resolve a vague clinical request."
  return 2      # expected T3 failure: no lease, nothing delivered
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
    say "$mode: bringing the stack up ($SIM_MODE, remote cloud $CLOUD)"
    NIMBUS_CLOUD_URL="$CLOUD" ./run_all.sh "$SIM_MODE" --system-mode "$mode" || {
      say "$mode: run_all.sh failed to start the stack"; return 1; }
  fi

  if ! wait_cloud_up; then
    say "$mode: cloud never came up on $CLOUD"; return 1
  fi
  curl -fs "$CLOUD/fleet" 2>/dev/null | python3 -c "import sys,json;d=json.load(sys.stdin);print(f\"[T3] running mode={d.get('mode')} llm={d.get('llm')} pack={d.get('pack')}\")" 2>/dev/null || true
  sleep 3

  reset_memory "$mode"

  local rc CLARIFIED="n/a"
  if [ "$mode" = "C3" ]; then
    run_c3_config; rc=$?
  else
    run_llm_config "$mode"; rc=$?
  fi

  local outdir="$RESULTS/$mode"; mkdir -p "$outdir"
  cp -f "$EAST_LOG" "$outdir/edge_east-wing.log" 2>/dev/null || true
  cp -f "$WEST_LOG" "$outdir/edge_west-wing.log" 2>/dev/null || true
  cp -f "$CLOUD_LOG" "$outdir/cloud.log" 2>/dev/null || true

  if [ "$mode" = "C3" ]; then
    case "$rc" in
      2) say "C3: FAILED as expected (logs in ${outdir#$ROOT/})"; RESULT[$mode]="FAILED (expected: no clarification in the no-LLM baseline)" ;;
      3) say "C3: did NOT fail (unexpected)"; RESULT[$mode]="LEASED (unexpected for T3)" ;;
      *) say "C3: error bringing up/submitting"; RESULT[$mode]="ERROR" ;;
    esac
  else
    case "$rc" in
      0) say "$mode: COMPLETED after clarification=$CLARIFIED (logs in ${outdir#$ROOT/})"; RESULT[$mode]="COMPLETED (clarified=$CLARIFIED)" ;;
      2) local line; line="$(grep -hE 'Final Mission Status:' "$EAST_LOG" "$WEST_LOG" 2>/dev/null | tail -1)"
         say "$mode: ended NOT completed -> ${line:-unknown}"; RESULT[$mode]="${line:-NOT_COMPLETED}" ;;
      *) say "$mode: TIMED OUT after ${TIMEOUT}s"; RESULT[$mode]="TIMEOUT" ;;
    esac
  fi

  say "$mode: cleanup"
  ./stop_all.sh >/dev/null 2>&1 || true
  sleep 3
}

declare -A RESULT
for mode in "${CONFIGS[@]}"; do
  case "$mode" in C1|C2|C3) ;; *) say "skipping unknown config '$mode'"; continue ;; esac
  run_config "$mode"
done

# T3 holds (hypothesis confirmed) iff C1/C2 completed and C3 did not.
say "================= T3 SUMMARY ================="
for mode in "${CONFIGS[@]}"; do
  printf '   %-3s : %s\n' "$mode" "${RESULT[$mode]:-SKIPPED}"
done

ok=0
for mode in "${CONFIGS[@]}"; do
  r="${RESULT[$mode]:-}"
  case "$mode" in
    C1|C2) [[ "$r" == COMPLETED* ]] || ok=1 ;;
    C3)    [[ "$r" == FAILED* ]]   || ok=1 ;;
  esac
done
if [ "$ok" = 0 ]; then
  say "T3 CONFIRMED: C1/C2 handled the vague request via follow-up; C3 could not."
else
  say "T3 NOT fully confirmed -- see the per-config results above."
fi
echo "   Per-config logs: ${RESULTS#$ROOT/}/<C1|C2|C3>/"
exit "$ok"
