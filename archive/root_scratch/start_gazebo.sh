#!/bin/bash
source /root/addModelPath.sh
cd /root/nimbus/simulation/hospital_world
export HEADLESS=1
exec ./launch_hospital_six_racecars.sh
