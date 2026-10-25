"""Incremental Ackermann keyboard teleoperation through the SDK only."""
from __future__ import annotations
import argparse, select, sys, termios, tty
from .runtime import RobotRuntime

def _key():
    key=sys.stdin.read(1)
    if key != "\x1b": return key.lower()
    if select.select([sys.stdin],[],[],.05)[0] and sys.stdin.read(1)=="[" and select.select([sys.stdin],[],[],.05)[0]: return {"A":"w","B":"s","C":"d","D":"a"}.get(sys.stdin.read(1),"")
    return ""

def main():
    parser=argparse.ArgumentParser(description="Ground robot SDK keyboard control")
    parser.add_argument("--speed",type=float,default=.2); parser.add_argument("--steering-step-degrees",type=float,default=5.)
    parser.add_argument("--robot-name",default="ground_robot"); parser.add_argument("--namespace",default="")
    args=parser.parse_args()
    if not sys.stdin.isatty() or args.steering_step_degrees <= 0: raise SystemExit("interactive terminal and positive steering step required")
    with RobotRuntime(robot_name=args.robot_name, namespace=args.namespace) as runtime:
        steering=0.; drive=0.; limit=runtime.limits.max_steering_angle
        settings=termios.tcgetattr(sys.stdin)
        def send(): runtime.motion.drive(drive,steering,source="keyboard")
        print(f"Robot {runtime.identity.robot_name} ({runtime.identity.namespace or 'global'}): A/D increment {args.steering_step_degrees:g} deg (limit +/-{limit:.2f} rad); W/S: drive; R: straight; X: stop; Q: quit")
        try:
            tty.setcbreak(sys.stdin.fileno())
            while True:
                if not select.select([sys.stdin],[],[],.25)[0]: continue
                key=_key()
                if key=="w": drive=abs(args.speed); send()
                elif key=="s": drive=-abs(args.speed); send()
                elif key in ("a","d"):
                    import math
                    step=math.radians(args.steering_step_degrees)*(1 if key=="a" else -1); steering=max(-limit,min(limit,steering+step)); send(); print(f"Steering {math.degrees(steering):+.1f} deg")
                elif key=="r": steering=0.; send()
                elif key in ("x"," "): drive=steering=0.; runtime.motion.stop("keyboard")
                elif key=="q": break
        finally: termios.tcsetattr(sys.stdin,termios.TCSADRAIN,settings)

if __name__=="__main__": main()
