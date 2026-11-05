#!/usr/bin/env bash
# Run the WHOLE stack (Gazebo + cloud + both edges) inside the simulator image.
#
#   docker pull kautuk05/ds252-icc:gazebo_hospital
#   ./scripts/docker_gazebo.sh              # Gazebo GUI via X11/WSLg (Linux / WSL2)
#   HEADLESS=1 ./scripts/docker_gazebo.sh   # no GUI window (use the web pages only)
#
# The repo is mounted at /root/nimbus. Ports 8000 (cloud/apps) and 8090 are published to the host.
set -euo pipefail
cd "$(dirname "$0")/.."
IMAGE="${IMAGE:-kautuk05/ds252-icc:gazebo_hospital}"
GUI=()
if [ "${HEADLESS:-0}" != 1 ]; then
  GUI=(-e DISPLAY="${DISPLAY:-:0}" -e WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}" -e XDG_RUNTIME_DIR=/mnt/wslg/runtime-dir
       -v /tmp/.X11-unix:/tmp/.X11-unix -v /mnt/wslg:/mnt/wslg)
fi
exec docker run --rm -it -p 8000:8000 -p 8090:8090 "${GUI[@]}" -e HEADLESS="${HEADLESS:-0}" \
  -v "$PWD":/root/nimbus -w /root/nimbus "$IMAGE" bash -lc './run_all.sh gazebo; echo; echo "Ctrl-C to stop."; tail -f logs/cloud.log logs/edge_east-wing.log logs/edge_west-wing.log'
