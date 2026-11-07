#!/usr/bin/env bash
# =============================================================================
# T6.sh -- one-touch T6: "Ward declared infectious mid-mission".  C1, C2 >> C3.
#
#   PRD scenario:
#     "Ward 2C is now isolation. Get the spare pumps out, hold routine deliveries
#      there, urgent ones can still go in."  The cloud re-prioritises, sends robots
#      (not staff) to move pumps out of 2C, holds routine deliveries to 2C, still
#      lets urgent ones in, and informs the ward and the operator.
#
#   Ward 2C = west_waiting_approach, which is seeded with 2 spare infusion pumps
#   (pack fleet.json places), so there is something to evacuate. The pack's
#   "isolation" restriction has effect=hold and exempt_priorities=[urgent]:
#   i.e. hold routine, let urgent in -- exactly the PRD policy.
#
#   What we expect (C1, C2 >> C3):
#     * C1 / C2 (cloud LLM): parse the free-text declaration, apply the 2C
#       isolation restriction, DISPATCH robots to move the spare pumps out of 2C,
#       HOLD a routine delivery to 2C, still LEASE an urgent delivery to 2C, and
#       post operator/ward notices.
#     * C3 (no LLM): cannot carry out the nuanced re-prioritisation from the
#       natural-language command -- in particular the "get the spare pumps out"
#       evacuation and the operator/ward informing that need the model.
#
#   Cloud (project rule -- C1/C2 ALWAYS use the deployed endpoints):
#     C1 -> deployed qrgdidp9te (real Bedrock)
#     C2 -> deployed xkczmnuqla
#     C3 -> local classical cloud on the real hospital_sim6 pack
#   Edge: scripts/edge_sim.py (one per wing, kinematic) so dispatched work executes.
#   This is a REAL run against the deployed cloud for C1/C2 (creates real tasks;
#   shows in the operator GUI). It uses the REAL map -- no pack/map change.
#
# Usage:
#   ./T6.sh                         # C1 C2 C3, one pass
#   T6_MODES="C1 C3" ./T6.sh        # a subset
#   CLOUD_C1=... CLOUD_C2=... ./T6.sh
# =============================================================================
set -uo pipefail
cd "$(dirname "$0")"
ROOT="$PWD"

MODES=(${T6_MODES:-C1 C2 C3})
CLOUD_C1="${CLOUD_C1:-https://qrgdidp9te.execute-api.ap-south-1.amazonaws.com}"
CLOUD_C2="${CLOUD_C2:-https://xkczmnuqla.execute-api.ap-south-1.amazonaws.com}"
PACE="${T6_PACE:-20}"
SETTLE="${T6_SETTLE:-25}"                 # seconds to let the cloud react + robots execute
RESULTS="$ROOT/T6_results"
mkdir -p "$RESULTS"

# The PRD utterances.
DECLARE="Ward 2C is now isolation. Get the spare pumps out, hold routine deliveries there, urgent ones can still go in."
ROUTINE="Deliver a blanket to 2C, routine."
URGENT="Deliver a blanket to 2C, urgent."

CLOUD=""   # set per-config
CLOUD_PID=""

say()  { printf '\n\033[1m[T6] %s\033[0m\n' "$*"; }
line() { printf '    %s\n' "$*"; }
normalize_url() { local u="$1"; case "$u" in http://*|https://*) ;; *) u="http://$u" ;; esac; printf '%s' "${u%/}"; }
is_local_url()  { case "$1" in *127.0.0.1*|*localhost*) return 0 ;; *) return 1 ;; esac; }

# curl helpers (no jq dependency; parse with python3)
post_json() { curl -fsS -m 30 -X POST "$CLOUD$1" -H 'Content-Type: application/json' -d "$2" 2>/dev/null; }
get_json()  { curl -fsS -m 30 "$CLOUD$1" 2>/dev/null; }
pyget() { python3 -c "import sys,json
try: d=json.load(sys.stdin)
except Exception: print(''); sys.exit()
import functools
cur=d
for k in '''$1'''.split('.'):
    if k=='' : continue
    if isinstance(cur,dict): cur=cur.get(k,'')
    else: cur=''
print(cur if cur is not None else '')"; }

reset_memory() { # reset FR-19 long-term memory on a cloud url ($1)
  if curl -fsS -m 25 -X POST "${1%/}/reset-memory" -H 'Content-Type: application/json' -d '{}' >/dev/null 2>&1; then
    line "reset ok   : ${1%/}"
  else
    line "reset FAIL : ${1%/} (continuing)"
  fi
}

# --- reset BOTH deployed clouds' long-term memory up front (determinism) ---------------------
say "resetting long-term memory on deployed C1 and C2 (top of script)"
reset_memory "$CLOUD_C1"
reset_memory "$CLOUD_C2"

submit() { # submit free text ($1); echo "<status> <task_id>"
  local resp status tid
  resp="$(post_json /requests "{\"text\":\"$1\",\"message_id\":\"t6-$(date +%s%N)\"}")"
  status="$(printf '%s' "$resp" | pyget status)"
  tid="$(printf '%s' "$resp" | pyget task_id)"
  printf '%s %s' "${status:-ERR}" "${tid:-}"
}

# Probe a PUBLIC route (/map.json is public even when auth is on) so readiness never
# depends on auth; retry a few routes to be safe.
wait_cloud_up() {
  for _ in $(seq 1 60); do
    for p in /health /map.json /fleet; do
      curl -fs -m 5 "$CLOUD$p" >/dev/null 2>&1 && return 0
    done
    sleep 1
  done
  return 1
}

start_local_c3() {
  local port=8000
  CLOUD="$(normalize_url "127.0.0.1:$port")"
  say "C3: starting LOCAL classical cloud (NIMBUS_MODE=C3, real hospital_sim6 pack) on $CLOUD"
  # NIMBUS_AUTH_DISABLED=1: the local dev cloud fails closed on auth (GET /fleet, /tasks,
  # /inventory, /notifications need an operator token). This flag opens them for local dev
  # (exactly its intended use). The deployed C1/C2 are unaffected (auth is at the gateway).
  NIMBUS_MODE=C3 NIMBUS_PACK=hospital_sim6 NIMBUS_LLM=fake NIMBUS_BACKEND=local NIMBUS_AUTH_DISABLED=1 PORT="$port" \
    DATA_DIR="$RESULTS/c3_data" python3 -u server.py >"$RESULTS/c3_cloud.log" 2>&1 &
  CLOUD_PID=$!
  wait_cloud_up || { say "C3: local cloud did not come up"; return 1; }
  reset_memory "$CLOUD"
}

EDGE_PIDS=()
start_edges() {
  EDGE_PIDS=()
  python3 -u scripts/edge_sim.py --api "$CLOUD" --cluster east-wing --complete --pace "$PACE" --interval 1 \
    >"$RESULTS/${1}_edge_east.log" 2>&1 & EDGE_PIDS+=($!)
  python3 -u scripts/edge_sim.py --api "$CLOUD" --cluster west-wing --complete --pace "$PACE" --interval 1 \
    >"$RESULTS/${1}_edge_west.log" 2>&1 & EDGE_PIDS+=($!)
  sleep 4   # let both wings heartbeat so the cloud sees healthy clusters
}
stop_edges() { for p in "${EDGE_PIDS[@]:-}"; do kill "$p" 2>/dev/null || true; done; EDGE_PIDS=(); }
stop_local_cloud() { [ -n "$CLOUD_PID" ] && { kill "$CLOUD_PID" 2>/dev/null || true; CLOUD_PID=""; }; }

cleanup() { stop_edges; stop_local_cloud; }
trap cleanup EXIT INT TERM

declare -A VERDICT

run_config() {
  local mode="$1" out="$RESULTS/$mode"; mkdir -p "$out"
  CLOUD_PID=""
  case "$mode" in
    C1) CLOUD="$(normalize_url "$CLOUD_C1")" ;;
    C2) CLOUD="$(normalize_url "$CLOUD_C2")" ;;
    C3) start_local_c3 || { VERDICT[$mode]="cloud failed to start"; return 1; } ;;
  esac
  say "===== Configuration $mode  (cloud $CLOUD) ====="
  if [ "$mode" != C3 ]; then reset_memory "$CLOUD"; fi

  start_edges "$mode"

  # Baseline: how many spare pumps are in 2C (west_waiting_approach) before isolation.
  local before; before="$(get_json /inventory | python3 -c "import sys,json
try: d=json.load(sys.stdin)
except Exception: print(''); sys.exit()
print(json.dumps(d))" 2>/dev/null || echo '')"
  printf '%s' "$before" >"$out/inventory_before.json"

  # 1) Declare the ward infectious (the PRD operator utterance).
  say "$mode: declaring -> \"$DECLARE\""
  local draw; draw="$(post_json /requests "{\"text\":\"$DECLARE\",\"message_id\":\"t6-decl-$(date +%s%N)\"}")"
  printf '%s' "$draw" >"$out/declaration_response.json"
  line "declaration response: $(printf '%s' "$draw" | pyget status) $(printf '%s' "$draw" | pyget task_id)"

  # 2) A ROUTINE delivery to 2C  -> should be HELD under isolation.
  say "$mode: routine delivery to 2C (expect HELD)"
  read -r r_status r_tid < <(submit "$ROUTINE")
  line "routine submit: status=$r_status task=$r_tid"

  # 3) An URGENT delivery to 2C  -> should still be LET IN (exempt).
  say "$mode: urgent delivery to 2C (expect LEASED / allowed)"
  read -r u_status u_tid < <(submit "$URGENT")
  line "urgent submit: status=$u_status task=$u_tid"

  say "$mode: letting the cloud react + robots execute (${SETTLE}s)"
  sleep "$SETTLE"

  # ---- Observe the four PRD behaviours ----
  local mapj restr notif tasks
  mapj="$(get_json /map.json)";   printf '%s' "$mapj"  >"$out/map.json"
  notif="$(get_json /notifications)"; printf '%s' "$notif" >"$out/notifications.json"
  tasks="$(get_json /tasks)";     printf '%s' "$tasks" >"$out/tasks.json"
  get_json /inventory >"$out/inventory_after.json" 2>/dev/null || true

  # (a) is 2C under an isolation restriction now?
  local iso; iso="$(printf '%s' "$mapj" | python3 -c "import sys,json
try: d=json.load(sys.stdin)
except Exception: print('no'); sys.exit()
r=d.get('restrictions') or {}
hit=[z for z in r if '2c' in z.lower() or 'west_waiting' in z.lower()]
print('yes:'+','.join(hit) if hit else 'no')" 2>/dev/null)"
  line "(a) 2C restriction active : $iso"

  # (b) were robots dispatched to move the spare pumps OUT of 2C? (a task sourced at 2C moving a pump)
  local evac; evac="$(printf '%s' "$tasks" | python3 -c "import sys,json
try: d=json.load(sys.stdin)
except Exception: print('unknown'); sys.exit()
ts=d.get('tasks',[])
ev=[t for t in ts if 'pump' in json.dumps(t).lower() and any(k in json.dumps(t).lower() for k in ('2c','west_waiting','out','evac','move','remove'))]
print(f'{len(ev)} evac-like task(s)' if ev else 'none found')" 2>/dev/null)"
  line "(b) spare pumps evacuation: $evac"

  # (c) routine delivery HELD?  (d) urgent delivery allowed?
  local rstat ustat
  rstat="$([ -n "$r_tid" ] && get_json "/tasks/$r_tid" | pyget status || echo "$r_status")"
  ustat="$([ -n "$u_tid" ] && get_json "/tasks/$u_tid" | pyget status || echo "$u_status")"
  line "(c) routine->2C status    : $rstat   (expect held/queued/blocked)"
  line "(d) urgent ->2C status    : $ustat   (expect leased/completed)"

  # (e) operator / ward informed?
  local nnotes; nnotes="$(printf '%s' "$notif" | python3 -c "import sys,json
try: d=json.load(sys.stdin)
except Exception: print('0'); sys.exit()
ns=d.get('notifications',d if isinstance(d,list) else [])
print(len(ns) if isinstance(ns,list) else 0)" 2>/dev/null)"
  line "(e) operator/ward notices : ${nnotes:-0}"

  VERDICT[$mode]="restriction=$iso | evac=$evac | routine=$rstat | urgent=$ustat | notices=${nnotes:-0}"

  say "$mode: cleanup"
  stop_edges
  [ "$mode" = C3 ] && stop_local_cloud
  line "evidence saved under ${out#$ROOT/}/"
}

for mode in "${MODES[@]}"; do
  case "$mode" in C1|C2|C3) run_config "$mode" || true ;; *) say "skipping unknown config '$mode'" ;; esac
done

say "================= T6 SUMMARY  (ideal: C1, C2 >> C3) ================="
for mode in "${MODES[@]}"; do
  printf '   %-3s : %s\n' "$mode" "${VERDICT[$mode]:-SKIPPED}"
done
echo "   Per-config evidence: ${RESULTS#$ROOT/}/<C1|C2|C3>/"
