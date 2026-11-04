#!/bin/bash
source /root/addModelPath.sh
cd /root/nimbus/simulation/hospital_world
export DISPLAY=:0
export WAYLAND_DISPLAY=wayland-0
export XDG_RUNTIME_DIR=/mnt/wslg/runtime-dir
export HEADLESS=0
exec ./launch_hospital_six_racecars.sh
