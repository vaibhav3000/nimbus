#!/usr/bin/env bash
# Stop everything started by run_all.sh
cd "$(dirname "$0")"
ROOT="$PWD"
LOGS="${LOGS_DIR:-logs}"
DATA="${DATA_DIR:-$ROOT/data}"

# A clean simulation restart must not consume a lease publication left by a
# previous local-cloud run.  Keep the decision evidence and lease-version
# fence intact: the latter prevents a still-visible remote lease from being
# executed again after Edge restarts.  Only queued transport records are
# cleared here.
clear_queues() {
  local cleared_cloud=0 cleared_edge=0

  # The file-backed cloud uses this append-only file as its cloud -> edge
  # message queue.  Truncating it is safe only after the robot processes have
  # been stopped above.
  if [ -f "$DATA/outbox.jsonl" ]; then
    cleared_cloud=$(awk 'NF { n++ } END { print n+0 }' "$DATA/outbox.jsonl")
    : > "$DATA/outbox.jsonl"
  fi

  # Edge's SQLite outbox is edge -> cloud store-and-forward traffic.  Do not
  # erase leases, lease_versions, or seen_messages: they are the replay fence
  # for an external/deployed cloud whose outbox this machine cannot purge.
  for db in "$DATA"/edge_*.db "$ROOT"/edge_*.db; do
    [ -f "$db" ] || continue
    count=$(python3 - "$db" <<'PY'
import sqlite3
import sys

try:
    with sqlite3.connect(sys.argv[1]) as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='outbox'"
        ).fetchone()
        if not exists:
            print(0)
        else:
            pending = conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
            conn.execute("DELETE FROM outbox")
            print(pending)
except sqlite3.Error:
    print(0)
PY
)
    cleared_edge=$((cleared_edge + count))
  done

  # In an AWS deployment, IoT Core may place edge -> cloud records in SQS.
  # Purge it only when the operator has explicitly configured that queue for
  # this shell; SQS itself enforces its normal purge-rate limit.
  if [ -n "${NIMBUS_SQS_QUEUE_URL:-}" ]; then
    if command -v aws >/dev/null 2>&1; then
      if aws sqs purge-queue --queue-url "$NIMBUS_SQS_QUEUE_URL" --region "${AWS_REGION:-ap-south-1}" >/dev/null 2>&1; then
        [ "${1:-}" = quiet ] || echo "purged configured SQS queue"
      else
        [ "${1:-}" = quiet ] || echo "warning: could not purge configured SQS queue" >&2
      fi
    else
      [ "${1:-}" = quiet ] || echo "warning: NIMBUS_SQS_QUEUE_URL is set but AWS CLI is unavailable" >&2
    fi
  fi

  [ "${1:-}" = quiet ] || echo "cleared queued messages: cloud=$cleared_cloud, edge=$cleared_edge"
}

if [ -f "$LOGS/pids" ]; then while read -r p; do [ -n "$p" ] && kill "$p" 2>/dev/null; done < "$LOGS/pids"; : > "$LOGS/pids"; fi
[ -n "${LOGS_DIR:-}" ] && { clear_queues "${1:-}"; [ "${1:-}" = quiet ] || echo "stopped (own pids only)"; exit 0; }
for pat in "edge/run_supervisor.py" "simulation/lite/viewer.py" "server.py" "ros_gz_bridge|ros_gz_image" "ign gazebo"; do
  for p in $(pgrep -f "^(python3?|ros2|ign|/[^ ]*)[^|]*($pat)" 2>/dev/null); do
    [ "$p" = "$$" ] || [ "$p" = "$PPID" ] || kill "$p" 2>/dev/null
  done
done

pkill -f ign
pkill -f ros
# kill all the processes which were returned by the ps aux | grep run_supervisor.py command
ps aux | grep run_supervisor.py | grep -v grep | awk '{print $2}' | xargs kill -9

clear_queues "${1:-}"
[ "${1:-}" = quiet ] || echo "stopped"
exit 0
