#!/usr/bin/env python3
"""Drive a square with high-level odometry-based `goto` tasks."""

from pathlib import Path
import sys
import time

# Allow direct execution with `python3 examples/drive_square.py`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ground_robot_sdk import RobotRuntime


SIDE_METRES = 3


def wait_for_task(runtime: RobotRuntime, command_id: str) -> None:
    """Wait for a tracked task and surface a rejected or failed goal."""
    while True:
        task = runtime.commands.status(command_id)
        if task is None:
            raise RuntimeError(f"Task {command_id} disappeared")
        if task.status.value == "completed":
            return
        if task.status.value in {"failed", "cancelled", "rejected"}:
            raise RuntimeError(f"goto failed: {task.status.value}: {task.reason}")
        time.sleep(0.1)


def main() -> None:
    with RobotRuntime() as runtime:
        # Coordinates are absolute in the runtime's startup frame. The
        # NavigationController handles pose, heading, and Ackermann steering.
        corners = (
            (SIDE_METRES, 0.0),
            (SIDE_METRES, SIDE_METRES),
            (0.0, SIDE_METRES),
            (0.0, 0.0),
        )
        for number, (x, y) in enumerate(corners, start=1):
            print(f"Going to corner {number}: ({x:.2f}, {y:.2f})")
            task = runtime.commands.goto(x, y, frame="world", speed=1.0)
            wait_for_task(runtime, task.command_id)

        runtime.commands.stop()


if __name__ == "__main__":
    main()
