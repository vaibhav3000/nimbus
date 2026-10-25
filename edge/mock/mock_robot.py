"""
Nimbus Mock Robot (Person 1 Emulation for Testing)
Emulates PiCar-X / MIT-RACECAR navigation stack and sub-task command primitives:
go_to, wait, stop, photo, beep, report.
Incorporates Person 1's topology waypoints (hospital_topology.json / topology_waypoints.json).
"""

import json
from pathlib import Path
import time
from typing import Any, Dict, List, Optional


# First-floor hospital topology waypoints from Person 1 (world frame, metres)
DEFAULT_TOPOLOGY_WAYPOINTS = {
    "lift_lobby": [0.0, 18.0],
    "north_hall": [0.0, 12.0],
    "west_north_gate": [-4.5, 14.0],
    "east_north_gate": [4.5, 14.0],
    "west_waiting_approach": [-4.5, 8.0],
    "east_waiting_approach": [4.5, 8.0],
    "west_mid_hall": [-4.5, 3.0],
    "east_mid_hall": [4.5, 3.0],
    "nurse_station_approach": [0.0, 5.0],
    "west_south_hall": [-4.5, -8.0],
    "east_south_hall": [4.5, -8.0],
    "south_equipment_approach": [0.0, -15.0],
    "south_room_approach": [0.0, -28.0],
}


class MockRobot:
    def __init__(
        self,
        robot_id: str = "R1",
        initial_pos: Optional[list] = None,
        current_zone: Optional[str] = None,
        waypoints: Optional[Dict[str, List[float]]] = None,
    ):
        self.robot_id = robot_id
        self.waypoints = dict(DEFAULT_TOPOLOGY_WAYPOINTS)
        if waypoints:
            self.waypoints.update(waypoints)

        # Default zone resolution matching hospital topology clusters
        if current_zone:
            self.current_zone = current_zone
        elif robot_id in ("R4", "R5", "R6") or "west" in robot_id.lower():
            self.current_zone = "west_waiting_approach"
        else:
            self.current_zone = "east_waiting_approach"

        # Initial pose matching topology waypoint
        if initial_pos:
            self.pose = list(initial_pos)
            if len(self.pose) == 2:
                self.pose.append(0.0)
        elif self.current_zone in self.waypoints:
            wp = self.waypoints[self.current_zone]
            self.pose = [wp[0], wp[1], 0.0]
        else:
            self.pose = [0.0, 0.0, 0.0]

        self.status = "IDLE"  # IDLE, RUNNING, BLOCKED, COMPLETED
        self.battery_pct = 95.0
        self.current_cmd_id = None
        self.history = []

    def execute_command(self, cmd: Dict[str, Any]) -> Dict[str, Any]:
        """
        Executes a received RobotCommand primitive.
        Returns execution status report.
        """
        cmd_id = cmd["cmd_id"]
        action = cmd["action"]
        params = cmd.get("params", {})
        self.current_cmd_id = cmd_id
        self.status = "RUNNING"

        record = {
            "cmd_id": cmd_id,
            "action": action,
            "params": params,
            "start_time": time.time(),
        }

        if action == "go_to":
            target_name = params.get("to") or params.get("target_name") or "target"
            target = params.get("target")

            # Resolve coordinate from topology waypoints if not explicitly passed
            if target is None and target_name in self.waypoints:
                target = self.waypoints[target_name]
            if target is None:
                target = self.pose[:2]

            self.pose[0] = target[0]
            self.pose[1] = target[1]
            if len(target) > 2:
                self.pose[2] = target[2]

            if target_name and target_name != "target":
                self.current_zone = target_name

            self.status = "COMPLETED"
            result_msg = f"Arrived at {self.current_zone} ({self.pose[:2]})"

        elif action == "wait":
            duration = params.get("duration_sec", params.get("seconds", 1.0))
            self.status = "COMPLETED"
            result_msg = f"Waited {duration}s"

        elif action == "photo":
            resolution = params.get("photo_resolution", "1280x720")
            self.status = "COMPLETED"
            result_msg = f"Photo captured at {self.pose} ({self.current_zone}) with res {resolution}"

        elif action == "beep":
            freq = params.get("beep_frequency_hz", 440)
            self.status = "COMPLETED"
            result_msg = f"Beeped at {freq}Hz"

        elif action == "stop":
            self.status = "IDLE"
            result_msg = "Emergency / reflex stop applied"

        elif action == "report":
            self.status = "IDLE"
            result_msg = f"Pose: {self.pose}, Zone: {self.current_zone}, Battery: {self.battery_pct}%"

        else:
            self.status = "FAILED"
            result_msg = f"Unknown action: {action}"

        record["end_time"] = time.time()
        record["status"] = self.status
        record["result_msg"] = result_msg
        self.history.append(record)

        return {
            "robot_id": self.robot_id,
            "cmd_id": cmd_id,
            "status": self.status,
            "pose": self.pose,
            "current_zone": self.current_zone,
            "message": result_msg,
            "timestamp": time.time(),
        }

    def get_status(self) -> Dict[str, Any]:
        """Returns current robot telemetry."""
        return {
            "robot_id": self.robot_id,
            "current_cmd_id": self.current_cmd_id,
            "status": self.status,
            "pose": self.pose,
            "current_zone": self.current_zone,
            "battery_pct": self.battery_pct,
            "timestamp": time.time(),
        }
