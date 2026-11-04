#!/usr/bin/env bash
# Stop everything started by run_all.sh
cd "$(dirname "$0")"
if [ -f logs/pids ]; then while read -r p; do [ -n "$p" ] && kill "$p" 2>/dev/null; done < logs/pids; : > logs/pids; fi
pkill -f "edge/run_supervisor.py" 2>/dev/null; pkill -f "simulation/lite/viewer.py" 2>/dev/null
pkill -f "python3 -u server.py" 2>/dev/null; pkill -f "ros_gz_bridge|ros_gz_image" 2>/dev/null
pkill -f "ign gazebo" 2>/dev/null
[ "${1:-}" = quiet ] || echo "stopped"
exit 0
