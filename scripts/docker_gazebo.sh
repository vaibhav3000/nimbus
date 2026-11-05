#!/usr/bin/env bash
# Run the WHOLE stack (Gazebo + cloud + both edges) inside the simulator image.
#
#   docker pull --platform linux/amd64 kautuk05/ds252-icc:gazebo_hospital
#   ./scripts/docker_gazebo.sh                 # auto: GUI on WSL2/Linux, headless on macOS
#   HEADLESS=1 ./scripts/docker_gazebo.sh      # force no Gazebo window
#   USE_XQUARTZ=1 ./scripts/docker_gazebo.sh   # macOS: GUI through XQuartz (xhost +localhost first)
#
# The repo is mounted at /root/nimbus. Ports 8000 (cloud/apps) and 8090 are published to the host.
# The image is linux/amd64: on Apple Silicon it runs under emulation (slow; give Docker >= 6 CPUs / 8 GB).
set -euo pipefail
cd "$(dirname "$0")/.."
IMAGE="${IMAGE:-kautuk05/ds252-icc:gazebo_hospital}"
OPTS=(--platform linux/amd64 --rm -p 8000:8000 -p 8090:8090)
[ -t 0 ] && OPTS+=(-it)
HEADLESS="${HEADLESS:-auto}"
if [ "$HEADLESS" = auto ]; then
  if [ -d /mnt/wslg ] || { [ "$(uname)" = Linux ] && [ -n "${DISPLAY:-}" ]; } || [ "${USE_XQUARTZ:-0}" = 1 ]; then HEADLESS=0; else HEADLESS=1; fi
fi
if [ "$HEADLESS" != 1 ]; then
  if [ -d /mnt/wslg ]; then   # WSL2 (WSLg)
    OPTS+=(-e DISPLAY="${DISPLAY:-:0}" -e WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}" -e XDG_RUNTIME_DIR=/mnt/wslg/runtime-dir -v /tmp/.X11-unix:/tmp/.X11-unix -v /mnt/wslg:/mnt/wslg)
  elif [ "${USE_XQUARTZ:-0}" = 1 ]; then   # macOS XQuartz
    OPTS+=(-e DISPLAY=host.docker.internal:0)
  else                        # native Linux X11
    OPTS+=(-e DISPLAY="$DISPLAY" -v /tmp/.X11-unix:/tmp/.X11-unix)
  fi
fi
echo "Gazebo GUI: $([ "$HEADLESS" = 1 ] && echo off '(headless; use http://127.0.0.1:8000 and :8090)' || echo on)"
exec docker run "${OPTS[@]}" -e HEADLESS="$HEADLESS" -e NIMBUS_SENSOR_TIMEOUT="${NIMBUS_SENSOR_TIMEOUT:-1.0}" -e NIMBUS_GOAL_TIMEOUT="${NIMBUS_GOAL_TIMEOUT:-270}" -v "$PWD":/root/nimbus -w /root/nimbus "$IMAGE" \
  bash -lc './run_all.sh gazebo; echo; echo "Ctrl-C to stop."; tail -f logs/cloud.log logs/edge_east-wing.log logs/edge_west-wing.log logs/gazebo.log'
