#!/usr/bin/env python3
"""Live edge/Gazebo validation harness.

Gazebo and its robot topics must already be running.  This program never falls
back to a mock adapter and exits non-zero unless readiness and the requested
terminal result are both observed.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time

ROOT=Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))

from edge.mock.sim_robot import SimRobot


def main() -> int:
    parser=argparse.ArgumentParser()
    parser.add_argument("--robot",default="R1")
    parser.add_argument("--target",nargs=2,type=float,metavar=("X","Y"),required=True)
    parser.add_argument("--validation-source",choices=["odometry","ground_truth"],default="ground_truth")
    parser.add_argument("--expect",choices=["COMPLETED","BLOCKED","SENSOR_UNAVAILABLE","LOCALIZATION_UNAVAILABLE"],default="COMPLETED")
    parser.add_argument("--disconnect",choices=["range","motion"],help="terminate one owned ROS-Gazebo bridge during navigation")
    parser.add_argument("--disconnect-after",type=float,default=0.5)
    args=parser.parse_args()
    robot=SimRobot(args.robot,use_gazebo=True,validation_source=args.validation_source)
    try:
        status=robot.get_status()
        print(json.dumps({"readiness":status},indent=2,default=str))
        if status["connection_state"] != "READY":
            print("Live Gazebo is not READY; no navigation command was sent.",file=sys.stderr)
            return 2
        if args.disconnect:
            process=robot.runtime.adapter.bridge_processes[args.disconnect]
            def disconnect():
                time.sleep(max(0.0,args.disconnect_after))
                os.killpg(process.pid,signal.SIGTERM)
            threading.Thread(target=disconnect,daemon=True).start()
        result=robot.execute_command({"cmd_id":"live-validation","action":"go_to","params":{"target":args.target,"speed":.25}})
        print(json.dumps(result,indent=2,default=str))
        if result.get("status") != args.expect:
            print(f"Expected {args.expect}, received {result.get('status')}",file=sys.stderr)
            return 1
        if args.expect == "COMPLETED" and not result.get("arrival_verified"):
            print("Completion lacked verified arrival.",file=sys.stderr)
            return 1
        return 0
    finally:
        robot.close()


if __name__ == "__main__": raise SystemExit(main())
