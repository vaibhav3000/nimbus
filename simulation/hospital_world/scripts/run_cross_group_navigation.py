#!/usr/bin/env python3
"""Drive robot_1 from Ward 2B to Ward 2C in the running hospital world."""

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Fortress uses IGN_PARTITION; ros_gz_bridge may use GZ_PARTITION.  Keep the
# bridge in the same transport partition as the already-running world.
if "IGN_PARTITION" in os.environ:
    os.environ["GZ_PARTITION"] = os.environ["IGN_PARTITION"]
elif "GZ_PARTITION" in os.environ:
    os.environ["IGN_PARTITION"] = os.environ["GZ_PARTITION"]

from ground_robot_sdk import RobotRuntime, SafetyConfig
from ground_robot_sdk.models import CommandStatus
from ground_robot_sdk.navigation import NavigationConfig
from topology.resolver import load_data, shortest_route, world_to_sdk_startup


START_NODE = "east_waiting_approach"  # Ward 2B
START_POSE = (4.5, 8.0, -1.57079632679)


def main() -> None:
    plan = shortest_route(START_NODE, "Ward 2C")
    route = plan["nodes"]
    topology, waypoint_document, _ = load_data()
    waypoints = [
        {
            "node": node,
            "world_xy": waypoint_document["waypoints"][node],
            "speed_cap": topology["nodes"][node].get("speed_cap", 0.25),
        }
        for node in route
    ]

    print("Route:", " -> ".join(route))
    print("Total topology cost:", plan["cost_m"], "m")

    with RobotRuntime(
        robot_name="robot_1",
        namespace="/robot_1",
        safety_config=SafetyConfig(
            obstacle_guard=True, minimum_front_distance=0.25
        ),
        navigation_config=NavigationConfig(goal_timeout=300.0),
    ) as robot:
        try:
            # The ROS↔Gazebo bridge starts asynchronously.  Do not submit a
            # goal until the SDK has received its first odometry message.
            ready_by = time.monotonic() + 30.0
            while not robot.state().health.odometry.fresh:
                if time.monotonic() >= ready_by:
                    raise RuntimeError(
                        "No /robot_1/odom received after 30 seconds; "
                        "ensure the hospital world is running."
                    )
                time.sleep(0.1)

            for index, waypoint in enumerate(waypoints[1:], start=1):
                x_sdk, y_sdk = world_to_sdk_startup(
                    tuple(waypoint["world_xy"]), START_POSE
                )
                speed = float(waypoint.get("speed_cap", 0.25))
                print(
                    f"[{index}/{len(waypoints)-1}] {waypoint['node']}: "
                    f"world={tuple(waypoint['world_xy'])}, sdk=({x_sdk:.2f}, {y_sdk:.2f})"
                )
                task = robot.commands.goto(x_sdk, y_sdk, frame="world", speed=speed)
                while True:
                    result = robot.commands.status(task.command_id)
                    if result.status == CommandStatus.COMPLETED:
                        break
                    if result.status in {
                        CommandStatus.FAILED,
                        CommandStatus.CANCELLED,
                        CommandStatus.REJECTED,
                    }:
                        raise RuntimeError(f"Navigation failed at {waypoint['node']}: {result}")
                    time.sleep(0.1)
        finally:
            robot.commands.stop()


if __name__ == "__main__":
    main()
