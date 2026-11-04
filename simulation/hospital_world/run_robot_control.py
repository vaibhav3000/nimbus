#!/usr/bin/env python3
import argparse, threading
from ground_robot_sdk.api import LocalAPIServer
from ground_robot_sdk import NavigationConfig, RobotRuntime, SafetyConfig
def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--host",default="127.0.0.1"); parser.add_argument("--port",type=int,default=8080); parser.add_argument("--robot-name",default="ground_robot"); parser.add_argument("--namespace",default=""); parser.add_argument("--validation-source",choices=["odometry","ground_truth"],default="odometry"); parser.add_argument("--sensor-timeout",type=float,default=1.0); args=parser.parse_args()
    nav=NavigationConfig(require_obstacle_feedback=True,obstacle_timeout=args.sensor_timeout,odometry_timeout=args.sensor_timeout,validation_source=args.validation_source)
    with RobotRuntime(robot_name=args.robot_name, namespace=args.namespace,safety_config=SafetyConfig(obstacle_guard=True),navigation_config=nav) as runtime:
        runtime.sensors._timeout=args.sensor_timeout
        api=LocalAPIServer(runtime,args.host,args.port); api.start(); print(f"Ground robot API at http://{api.address[0]}:{api.address[1]} readiness={runtime.readiness(require_ground_truth=args.validation_source == 'ground_truth')}")
        try: threading.Event().wait()
        except KeyboardInterrupt: pass
        finally: api.shutdown()
if __name__=="__main__": main()
