#!/usr/bin/env bash
# Stop everything started by run_all.sh
cd "$(dirname "$0")"
if [ -f logs/pids ]; then while read -r p; do [ -n "$p" ] && kill "$p" 2>/dev/null; done < logs/pids; : > logs/pids; fi
for pat in "edge/run_supervisor.py" "simulation/lite/viewer.py" "server.py" "ros_gz_bridge|ros_gz_image" "ign gazebo"; do
  for p in $(pgrep -f "^(python3?|ros2|ign|/[^ ]*)[^|]*($pat)" 2>/dev/null); do
    [ "$p" = "$$" ] || [ "$p" = "$PPID" ] || kill "$p" 2>/dev/null
  done
done
[ "${1:-}" = quiet ] || echo "stopped"
exit 0
