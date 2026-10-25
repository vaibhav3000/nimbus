#!/usr/bin/env python3
"""Record Ward 2B -> Ward 2C in an already-running two-RACECAR hospital."""
from __future__ import annotations

import argparse
import math
from pathlib import Path
import subprocess
import sys
import time

import cv2
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ground_robot_sdk import RobotRuntime, SafetyConfig  # noqa: E402
from ground_robot_sdk.models import CommandStatus  # noqa: E402
from topology.resolver import load_data, resolve_destination, shortest_route, world_to_sdk_startup  # noqa: E402


START_NODE = "east_waiting_approach"
START_POSE = (4.5, 8.0, -math.pi / 2.0)
EXPECTED_ROUTE = [
    "east_waiting_approach", "east_mid_hall", "nurse_station_approach",
    "west_mid_hall", "west_waiting_approach",
]


def set_scenario_start() -> None:
    """Place robot_1 at Ward 2B's physical waypoint; no SDF is modified."""
    pose = (
        'name: "robot_1" position { x: 4.5 y: 8.0 z: 0.0 } '
        'orientation { z: -0.7071067812 w: 0.7071067812 }'
    )
    result = subprocess.run(
        ["ign", "service", "-s", "/world/hospital/set_pose",
         "--reqtype", "ignition.msgs.Pose", "--reptype", "ignition.msgs.Boolean",
         "--timeout", "3000", "--req", pose],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
    )
    if result.returncode != 0 or "data: true" not in result.stdout:
        raise RuntimeError(f"could not place robot_1 at Ward 2B: {result.stdout.strip()}")


def decode(camera, label: str):
    if not camera.available or not camera.data:
        return None
    frame = cv2.imdecode(np.frombuffer(camera.data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        return None
    cv2.rectangle(frame, (0, 0), (frame.shape[1], 34), (0, 0, 0), -1)
    cv2.putText(frame, label, (10, 23), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1, cv2.LINE_AA)
    return frame


def await_goal(robot, command, label, writer) -> None:
    """Record actual camera frames while inspecting the asynchronous result."""
    while True:
        result = robot.commands.status(command.command_id)
        if result is None:
            raise RuntimeError("navigation command is no longer tracked")
        frame = decode(robot.commands.photo(), label)
        if frame is not None:
            writer.write(frame)
        if result.status == CommandStatus.COMPLETED:
            return
        if result.status in {CommandStatus.FAILED, CommandStatus.CANCELLED, CommandStatus.REJECTED}:
            raise RuntimeError(f"{result.status.value}: {result.reason}")
        time.sleep(0.10)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts" / "cross_group_navigation.mp4")
    parser.add_argument("--speed", type=float, default=0.8, help="capture speed; topology caps are not enforced by the SDK")
    args = parser.parse_args()
    if not 0 < args.speed <= 1.0:
        raise SystemExit("--speed must be in (0, 1.0]")

    route = shortest_route(START_NODE, "Ward 2C")
    if route["nodes"] != EXPECTED_ROUTE:
        raise RuntimeError(f"unexpected route: {route['nodes']}")
    if resolve_destination("Ward 2B")["physical_node"] != START_NODE:
        raise RuntimeError("Ward 2B no longer resolves to the expected node")
    set_scenario_start()
    time.sleep(1.0)

    topology, waypoint_document, _ = load_data()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    writer = None
    try:
        with RobotRuntime(
            robot_name="robot_1", namespace="/robot_1",
            safety_config=SafetyConfig(obstacle_guard=True, minimum_front_distance=0.25),
        ) as robot:
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline:
                state = robot.state()
                if state.health.odometry.fresh and state.health.range.fresh and state.camera.available:
                    break
                time.sleep(0.10)
            else:
                raise RuntimeError("fresh odometry, range, and camera data were not received")

            first = decode(robot.commands.photo(), "Ward 2B → Ward 2C")
            if first is None:
                raise RuntimeError("camera frame is unavailable")
            writer = cv2.VideoWriter(
                str(args.output), cv2.VideoWriter_fourcc(*"mp4v"), 10.0,
                (first.shape[1], first.shape[0]),
            )
            if not writer.isOpened():
                raise RuntimeError("OpenCV could not open the MP4 writer")
            writer.write(first)

            for source, target in zip(route["nodes"], route["nodes"][1:]):
                sdk_xy = world_to_sdk_startup(waypoint_document["waypoints"][target], START_POSE)
                print(f"{source} -> {target} at capture speed {args.speed:.2f} m/s", flush=True)
                task = robot.commands.goto(*sdk_xy, frame="world", speed=args.speed)
                await_goal(robot, task, f"{source} → {target}", writer)
            robot.commands.stop()
    finally:
        if writer is not None:
            writer.release()
    print(f"Recorded verified {route['cost_m']:.2f} m route: {args.output}")


if __name__ == "__main__":
    main()
