#!/usr/bin/env python3
import argparse, threading
from ground_robot_sdk.api import LocalAPIServer
from ground_robot_sdk.runtime import RobotRuntime
def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--host",default="127.0.0.1"); parser.add_argument("--port",type=int,default=8080); parser.add_argument("--robot-name",default="ground_robot"); parser.add_argument("--namespace",default=""); args=parser.parse_args()
    with RobotRuntime(robot_name=args.robot_name, namespace=args.namespace) as runtime:
        api=LocalAPIServer(runtime,args.host,args.port); api.start(); print(f"Ground robot API at http://{api.address[0]}:{api.address[1]}")
        try: threading.Event().wait()
        except KeyboardInterrupt: pass
        finally: api.shutdown()
if __name__=="__main__": main()
