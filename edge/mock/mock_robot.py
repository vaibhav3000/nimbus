"""
Nimbus Mock Robot (Person 1 Emulation for Testing)
Emulates PiCar-X / MIT-RACECAR navigation stack and sub-task command primitives:
go_to, wait, stop, photo, beep, report.
Incorporates Person 1's topology waypoints (hospital_topology.json / topology_waypoints.json).
Supports coordinate tracking, zone resolution, reflex emergency stopping,
transit corridor traversal, and dynamic obstacle / fault injection for tactical SLM autonomy (FR-1, FR-3, FR-4, FR-6).
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
    # Aliases from scenario_locations.json & legacy tests
    "central-supply": [0.0, 0.0],
    "corridor-2B": [10.0, 0.0],
    "corridor-2B-north": [10.0, 2.0],
    "2B-12": [14.0, 4.0],
    "ward-2B": [11.0, 4.0],
    "ward-2C-isolation": [25.0, 0.0],
    "pediatric-icu": [10.0, -5.0],
    "safe-parking-bay-1": [-1.0, 4.0],
    "safe-parking-bay-2": [11.0, -1.8],
    "safe-parking-bay-east": [20.0, 3.0],
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
        elif robot_id in ("R1", "robot_1"):
            self.current_zone = "north_hall"
        elif robot_id in ("R2", "robot_2"):
            self.current_zone = "nurse_station_approach"
        elif robot_id in ("R3", "robot_3"):
            self.current_zone = "east_waiting_approach"
        elif robot_id in ("R4", "robot_4"):
            self.current_zone = "west_waiting_approach"
        elif robot_id in ("R5", "robot_5"):
            self.current_zone = "west_mid_hall"
        elif robot_id in ("R6", "robot_6"):
            self.current_zone = "west_south_hall"
        else:
            self.current_zone = "north_hall"

        # Initial pose matching topology waypoint
        if initial_pos:
            self.pose = list(initial_pos)
            if len(self.pose) == 2:
                self.pose.append(0.0)
        elif robot_id in ("R1", "robot_1"):
            self.pose = [0.0, 12.0, 0.0]
        elif robot_id in ("R2", "robot_2"):
            self.pose = [0.0, 9.5, 0.0]
        elif robot_id in ("R3", "robot_3"):
            self.pose = [4.5, 8.0, 0.0]
        elif robot_id in ("R4", "robot_4"):
            self.pose = [-4.5, 8.0, 0.0]
        elif robot_id in ("R5", "robot_5"):
            self.pose = [-4.5, 3.0, 0.0]
        elif robot_id in ("R6", "robot_6"):
            self.pose = [-4.5, -8.0, 0.0]
        elif self.current_zone in self.waypoints:
            wp = self.waypoints[self.current_zone]
            self.pose = [wp[0], wp[1], 0.0]
        else:
            self.pose = [0.0, 12.0, 0.0]

        self.status = "IDLE"  # IDLE, RUNNING, BLOCKED, COMPLETED, STOPPED, FAILED
        self.battery_pct = 95.0
        self.has_fault = False
        self.fault_msg = None
        self.current_cmd_id = None
        self.history: List[Dict[str, Any]] = []

        # Obstacle simulation registry: {zone_or_corridor: {type, description, clears_after_waits, wait_count}}
        self.injected_obstacles: Dict[str, Dict[str, Any]] = {}

    def inject_obstacle(
        self,
        zone: str,
        obstacle_type: str = "transient",
        description: str = "Maintenance trolley blocking path",
        clears_after_waits: int = 1,
    ):
        """Injects a simulated dynamic or persistent obstacle into a zone or corridor."""
        self.injected_obstacles[zone] = {
            "type": obstacle_type,
            "description": description,
            "clears_after_waits": clears_after_waits,
            "wait_count": 0,
        }

    def clear_obstacles(self):
        """Clears all simulated obstacles."""
        self.injected_obstacles.clear()

    def set_fault(self, fault_msg: str = "Motor drive stall detected"):
        """Simulates a hardware or mechanical fault."""
        self.has_fault = True
        self.fault_msg = fault_msg

    def clear_fault(self):
        self.has_fault = False
        self.fault_msg = None

    def set_battery(self, pct: float):
        self.battery_pct = pct

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

        # Check for hardware fault or critically depleted battery
        if self.has_fault or self.battery_pct <= 5.0:
            self.status = "FAILED"
            msg = self.fault_msg or "Battery critical (<5%); emergency stop"
            record["end_time"] = time.time()
            record["status"] = self.status
            record["result_msg"] = msg
            self.history.append(record)
            return {
                "robot_id": self.robot_id,
                "cmd_id": cmd_id,
                "status": "FAILED",
                "pose": list(self.pose),
                "zone": self.current_zone,
                "has_fault": self.has_fault,
                "message": msg,
                "timestamp": time.time(),
            }

        if action == "go_to":
            target_name = params.get("to") or params.get("target_name") or "target"
            target = params.get("target")

            # Resolve coordinate from topology waypoints if not explicitly passed
            if target is None and target_name in self.waypoints:
                target = self.waypoints[target_name]
            if target is None:
                target = self.pose[:2]

            # Obstacle checking in target zone or transit corridor
            blocked_zone = None
            if target_name in self.injected_obstacles:
                blocked_zone = target_name
            elif self.current_zone in self.injected_obstacles:
                blocked_zone = self.current_zone
            elif (
                (self.current_zone in ("central-supply", "south_equipment_approach") and target_name in ("2B-12", "2B-01", "ward-2B")) or
                (self.current_zone in ("2B-12", "2B-01", "ward-2B") and target_name in ("central-supply", "south_equipment_approach"))
            ):
                if "corridor-2B" in self.injected_obstacles:
                    blocked_zone = "corridor-2B"
                elif "east_mid_hall" in self.injected_obstacles:
                    blocked_zone = "east_mid_hall"

            if blocked_zone:
                obs_info = self.injected_obstacles[blocked_zone]
                # If transient and waited enough, obstacle has cleared
                if obs_info["type"] == "transient" and obs_info["wait_count"] >= obs_info["clears_after_waits"]:
                    del self.injected_obstacles[blocked_zone]
                else:
                    self.status = "BLOCKED"
                    result_msg = f"Navigation blocked in {blocked_zone}: {obs_info['description']}"
                    record["end_time"] = time.time()
                    record["status"] = self.status
                    record["result_msg"] = result_msg
                    self.history.append(record)
                    return {
                        "robot_id": self.robot_id,
                        "cmd_id": cmd_id,
                        "status": "BLOCKED",
                        "pose": list(self.pose),
                        "zone": self.current_zone,
                        "blocked_zone": blocked_zone,
                        "obstacle_type": obs_info["type"],
                        "obstacle_description": obs_info["description"],
                        "message": result_msg,
                        "timestamp": time.time(),
                    }

            self.pose[0] = float(target[0])
            self.pose[1] = float(target[1])
            if len(target) > 2:
                self.pose[2] = float(target[2])

            if target_name and target_name != "target":
                self.current_zone = target_name

            self.status = "COMPLETED"
            result_msg = f"Arrived at {self.current_zone} ({self.pose[:2]})"

        elif action == "wait":
            duration = params.get("duration_sec", params.get("seconds", 1.0))
            self.status = "COMPLETED"
            result_msg = f"Waited {duration}s"

            # Increment wait counts on all active obstacles
            for obs in self.injected_obstacles.values():
                obs["wait_count"] += 1

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
            reason = params.get("reason", "Emergency / reflex stop applied")
            result_msg = f"Stop executed: {reason}"

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
            "pose": list(self.pose),
            "zone": self.current_zone,
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
            "pose": list(self.pose),
            "zone": self.current_zone,
            "current_zone": self.current_zone,
            "battery_pct": self.battery_pct,
            "has_fault": self.has_fault,
            "timestamp": time.time(),
        }
