#!/usr/bin/env python3
"""Record the verified Ward 2B-to-2C route from the robot camera.

This utility creates its scenario world in a temporary directory, starts an
isolated headless Fortress server, and removes it on exit. It never rewrites
the base hospital world, topology, controller, or SDK.
"""
from __future__ import annotations

import argparse
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

import cv2
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ground_robot_sdk import RobotRuntime, SafetyConfig  # noqa: E402
from ground_robot_sdk.models import CommandStatus  # noqa: E402
from ground_robot_sdk.navigation import NavigationConfig  # noqa: E402
from topology.resolver import (  # noqa: E402
    load_data,
    resolve_destination,
    shortest_route,
    world_to_sdk_startup,
)
from tools.generate_hospital_two_racecars import make_robot  # noqa: E402


START_NODE = "east_waiting_approach"
EXPECTED_ROUTE = [
    "east_waiting_approach",
    "east_mid_hall",
    "nurse_station_approach",
    "west_mid_hall",
    "west_waiting_approach",
]
# South-facing at Ward 2B's physical waypoint. The first camera/odometry sample
# establishes this pose as the SDK startup-local origin.
START_POSE = (4.5, 8.0, -math.pi / 2.0)


def make_scenario_world(path: Path, real_time_factor: float = 4.0) -> None:
    """Append one namespaced RACECAR at the physical Ward 2B waypoint."""
    tree = ET.parse(ROOT / "hospital.sdf")
    world = tree.getroot().find("world")
    source = ET.parse(ROOT / "models/robots/mit_racecar/model.sdf").getroot().find("model")
    if world is None or source is None:
        raise RuntimeError("hospital or MIT RACECAR SDF is malformed")
    physics = world.find("physics")
    if physics is not None:
        factor = physics.find("real_time_factor")
        if factor is not None:
            factor.text = str(real_time_factor)
    world.append(make_robot(source, "robot_1", *START_POSE))
    ET.indent(tree, space="  ")
    tree.write(path, encoding="utf-8", xml_declaration=True)


def gazebo_environment(partition: str) -> dict[str, str]:
    environment = dict(os.environ)
    resources = f"{ROOT}/models:{ROOT}/models/robots:{ROOT}/fuel_models"
    environment["IGN_GAZEBO_RESOURCE_PATH"] = resources
    environment["GZ_SIM_RESOURCE_PATH"] = resources
    environment["IGN_PARTITION"] = partition
    # Avoid inheriting a GUI X11 session into Ogre's headless EGL backend.
    environment.pop("DISPLAY", None)
    return environment


def wait_for_topics(environment: dict[str, str], timeout: float = 45.0) -> None:
    """Wait until the headless world advertises required robot sensors."""
    required = {"/robot_1/odom", "/robot_1/camera/image", "/robot_1/ultrasonic/scan"}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        topics = subprocess.run(
            ["ign", "topic", "-l"], env=environment, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
        ).stdout.splitlines()
        if required.issubset(topics):
            return
        time.sleep(1.0)
    raise RuntimeError("Gazebo did not publish required robot topics")


def frame_image(camera_frame, label: str):
    """Decode a real JPEG camera frame and add only route-status annotation."""
    if not camera_frame.available or not camera_frame.data:
        return None
    image = cv2.imdecode(np.frombuffer(camera_frame.data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return None
    cv2.rectangle(image, (0, 0), (image.shape[1], 34), (0, 0, 0), -1)
    cv2.putText(image, label, (10, 23), cv2.FONT_HERSHEY_SIMPLEX, 0.58, (255, 255, 255), 1, cv2.LINE_AA)
    return image


def record_until_terminal(robot, command, label: str, writer) -> None:
    """Poll the SDK CommandResult while recording live camera frames."""
    while True:
        result = robot.commands.status(command.command_id)
        if result is None:
            raise RuntimeError(f"command {command.command_id} disappeared from navigation history")
        image = frame_image(robot.commands.photo(), label)
        if image is not None:
            writer.write(image)
        if result.status == CommandStatus.COMPLETED:
            return
        if result.status in {CommandStatus.FAILED, CommandStatus.CANCELLED, CommandStatus.REJECTED}:
            raise RuntimeError(f"{result.command_type} {result.status.value}: {result.reason}")
        time.sleep(0.10)


def run(output: Path) -> None:
    route = shortest_route(START_NODE, "Ward 2C")
    if route["nodes"] != EXPECTED_ROUTE:
        raise RuntimeError(f"unexpected route: {route['nodes']}")
    source = resolve_destination("Ward 2B")
    destination = resolve_destination("Ward 2C")
    if source["physical_node"] != START_NODE:
        raise RuntimeError("Ward 2B mapping no longer matches the scenario start")

    topology, waypoint_document, _ = load_data()
    waypoints = waypoint_document["waypoints"]
    output.parent.mkdir(parents=True, exist_ok=True)
    partition = f"cross_group_record_{os.getpid()}_{int(time.time())}"
    environment = gazebo_environment(partition)
    # GazeboAdapter starts ros_gz bridge subprocesses. They inherit os.environ,
    # not the Popen environment below, so put both ends on this same partition.
    os.environ.update({key: environment[key] for key in (
        "IGN_GAZEBO_RESOURCE_PATH", "GZ_SIM_RESOURCE_PATH", "IGN_PARTITION",
    )})
    server = None
    writer = None
    try:
        with tempfile.TemporaryDirectory(prefix="hospital_cross_group_") as temporary:
            world = Path(temporary) / "ward_2b_to_2c.sdf"
            make_scenario_world(world)
            print(f"Starting 4x isolated Fortress scenario on {partition}", flush=True)
            log = open(Path(temporary) / "gazebo.log", "w", encoding="utf-8")
            server = subprocess.Popen(
                ["ign", "gazebo", "-r", "-s", "--headless-rendering", str(world)],
                env=environment, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
            )
            wait_for_topics(environment)
            print("Gazebo sensors are publishing; starting SDK bridge", flush=True)
            navigation = NavigationConfig(goal_timeout=90.0)
            safety = SafetyConfig(obstacle_guard=True, minimum_front_distance=0.25)
            with RobotRuntime(
                robot_name="robot_1", namespace="/robot_1",
                navigation_config=navigation, safety_config=safety,
            ) as robot:
                # Wait for real bridge data before issuing the first asynchronous goal.
                deadline = time.monotonic() + 30.0
                while time.monotonic() < deadline:
                    state = robot.state()
                    if state.health.odometry.fresh and state.health.range.fresh and state.camera.available:
                        break
                    time.sleep(0.10)
                else:
                    raise RuntimeError("SDK did not receive fresh odometry, range, and camera data")

                first_frame = frame_image(robot.commands.photo(), "Ward 2B → Ward 2C")
                if first_frame is None:
                    raise RuntimeError("camera frame became unavailable before recording")
                writer = cv2.VideoWriter(
                    str(output), cv2.VideoWriter_fourcc(*"mp4v"), 10.0,
                    (first_frame.shape[1], first_frame.shape[0]),
                )
                if not writer.isOpened():
                    raise RuntimeError("OpenCV could not open the MP4 writer")
                writer.write(first_frame)

                for source_node, target_node in zip(route["nodes"], route["nodes"][1:]):
                    source_cap = topology["nodes"][source_node]["speed_cap"]
                    target_cap = topology["nodes"][target_node]["speed_cap"]
                    speed = min(source_cap, target_cap)
                    sdk_xy = world_to_sdk_startup(waypoints[target_node], START_POSE)
                    print(
                        f"Navigating {source_node} -> {target_node} at {speed:.2f} m/s",
                        flush=True,
                    )
                    task = robot.commands.goto(*sdk_xy, frame="world", speed=speed)
                    record_until_terminal(robot, task, f"{source_node} → {target_node}", writer)
                    print(f"Reached {target_node}", flush=True)

                robot.commands.stop()
                print(f"Recorded {source['identifier']} → {destination['identifier']} ({route['cost_m']:.2f} m): {output}")
    finally:
        if writer is not None:
            writer.release()
        if server is not None and server.poll() is None:
            os.killpg(server.pid, signal.SIGTERM)
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(server.pid, signal.SIGKILL)
                server.wait(timeout=10)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path,
        default=ROOT / "artifacts" / "cross_group_navigation.mp4",
        help="MP4 output path (default: artifacts/cross_group_navigation.mp4)",
    )
    args = parser.parse_args()
    run(args.output)


if __name__ == "__main__":
    main()
