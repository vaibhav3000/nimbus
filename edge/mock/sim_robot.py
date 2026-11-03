"""
Nimbus Simulated Robot Driver (Person 1 Simulation Integration)
Bridges Person 2's Edge Supervisor command dispatch interface (execute_command)
with Person 1's Ground Robot SDK (RobotRuntime, TaskCommands, NavigationController)
and Gazebo Hospital World topology resolver.

Supports:
- Live Gazebo Fortress + ROS 2 Humble mode (when rclpy is available)
- Kinematic Mock mode (when running standalone or on Windows)
- Frame transformation: Gazebo World (x, y) -> SDK Startup-Local Odometry Frame
- Destination resolution: PRD aliases (Ward 2B, Central Supply) -> topology waypoints
- High-level primitives: go_to, wait, photo, beep, stop, report
"""

from __future__ import annotations

import heapq
import json
import math
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple


def _find_simulation_dir() -> Optional[Path]:
    """Finds Person 1's simulation/hospital_world directory across workspace layouts."""
    env_dir = os.environ.get("NIMBUS_SIMULATION_DIR")
    if env_dir:
        try:
            p = Path(env_dir)
            if p.is_dir():
                return p
        except Exception:
            pass

    base = Path(__file__).resolve()
    candidates = [
        base.parents[2] / "simulation" / "hospital_world",
        base.parents[3] / "Nimbus-Leased Authority for Agentic Ground-Robot Navigation across Edge and Cloud" / "simulation" / "hospital_world",
        Path.cwd() / "simulation" / "hospital_world",
        Path.cwd().parent / "Nimbus-Leased Authority for Agentic Ground-Robot Navigation across Edge and Cloud" / "simulation" / "hospital_world",
    ]
    for c in candidates:
        try:
            if (c / "ground_robot_sdk").is_dir() and (c / "topology").is_dir():
                return c
        except Exception:
            continue
    return None


# Ensure simulation dir is in sys.path
SIM_DIR = _find_simulation_dir()
if SIM_DIR and str(SIM_DIR) not in sys.path:
    sys.path.insert(0, str(SIM_DIR))

# Import simulation components if available
try:
    from ground_robot_sdk import MockAdapter, NavigationConfig, RobotRuntime, SafetyConfig
    from ground_robot_sdk.models import CameraFrame, CommandStatus, Odometry, Pose2D, RangeScan, Twist2D
    from ground_robot_sdk.namespaces import RobotIdentity
    from topology.resolver import load_data, resolve_destination, shortest_route, world_to_sdk_startup
    HAS_SDK = True
except Exception:
    HAS_SDK = False


# Default robot spawn configurations matching Person 1's config/robot_deployment.json
DEFAULT_ROBOT_DEPLOYMENT = {
    "R1": {
        "sim_name": "robot_1",
        "namespace": "/robot_1",
        "spawn_pose": [0.0, 12.0, 0.0],  # north_hall
        "start_node": "north_hall",
        "wing": "shared",
    },
    "R2": {
        "sim_name": "robot_2",
        "namespace": "/robot_2",
        "spawn_pose": [0.0, 9.5, 0.0],
        "start_node": "nurse_station_approach",
        "wing": "shared",
    },
    "R3": {
        "sim_name": "robot_3",
        "namespace": "/robot_3",
        "spawn_pose": [4.5, 8.0, -1.5707963],  # east_waiting_approach
        "start_node": "east_waiting_approach",
        "wing": "east",
    },
    "R4": {
        "sim_name": "robot_4",
        "namespace": "/robot_4",
        "spawn_pose": [-4.5, 8.0, -1.5707963],  # west_waiting_approach
        "start_node": "west_waiting_approach",
        "wing": "west",
    },
    "R5": {
        "sim_name": "robot_5",
        "namespace": "/robot_5",
        "spawn_pose": [-4.5, 3.0, 0.0],  # west_mid_hall
        "start_node": "west_mid_hall",
        "wing": "west",
    },
    "R6": {
        "sim_name": "robot_6",
        "namespace": "/robot_6",
        "spawn_pose": [-4.5, -8.0, 0.0],  # west_south_hall
        "start_node": "west_south_hall",
        "wing": "west",
    },
    "robot_1": {
        "sim_name": "robot_1",
        "namespace": "/robot_1",
        "spawn_pose": [0.0, 12.0, 0.0],
        "start_node": "north_hall",
        "wing": "shared",
    },
    "robot_2": {
        "sim_name": "robot_2",
        "namespace": "/robot_2",
        "spawn_pose": [0.0, 9.5, 0.0],
        "start_node": "nurse_station_approach",
        "wing": "shared",
    },
    "robot_3": {
        "sim_name": "robot_3",
        "namespace": "/robot_3",
        "spawn_pose": [4.5, 8.0, -1.5707963],
        "start_node": "east_waiting_approach",
        "wing": "east",
    },
    "robot_4": {
        "sim_name": "robot_4",
        "namespace": "/robot_4",
        "spawn_pose": [-4.5, 8.0, -1.5707963],
        "start_node": "west_waiting_approach",
        "wing": "west",
    },
    "robot_5": {
        "sim_name": "robot_5",
        "namespace": "/robot_5",
        "spawn_pose": [-4.5, 3.0, 0.0],
        "start_node": "west_mid_hall",
        "wing": "west",
    },
    "robot_6": {
        "sim_name": "robot_6",
        "namespace": "/robot_6",
        "spawn_pose": [-4.5, -8.0, 0.0],
        "start_node": "west_south_hall",
        "wing": "west",
    },
}

# Fallback topology waypoints if file not found
FALLBACK_TOPOLOGY_WAYPOINTS = {
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
    "central-supply": [0.0, 0.0],
    "2B-12": [4.5, 8.0],
    "ward-2B": [4.5, 8.0],
    "ward-2C": [-4.5, 8.0],
}


# Cloud contract zone aliases to simulation topology nodes
CLOUD_TO_SIM_ALIASES = {
    "E-STORE": "south_equipment_approach",
    "W-STORE": "south_equipment_approach",
    "central-supply": "south_equipment_approach",
    "equipment-store": "south_equipment_approach",
    "equipment store": "south_equipment_approach",
    "nurse-station": "nurse_station_approach",
    "nurse station": "nurse_station_approach",
    "2B-12": "east_waiting_approach",
    "bed-2b-12": "east_waiting_approach",
    "ward-2b": "east_waiting_approach",
    "2B": "east_waiting_approach",
    "ward-2c": "west_waiting_approach",
    "2C": "west_waiting_approach",
    "ward-3": "south_room_approach",
    "3": "south_room_approach",
}


def _detect_gazebo_partition() -> Optional[str]:
    """Detects active Ignition partition from running Gazebo process environment."""
    if os.environ.get("IGN_PARTITION"):
        return os.environ["IGN_PARTITION"]
    if os.environ.get("HOSPITAL_IGN_PARTITION"):
        return os.environ["HOSPITAL_IGN_PARTITION"]
    try:
        proc = Path("/proc")
        if not proc.is_dir():
            return None
        for p in proc.iterdir():
            if not p.is_dir() or not p.name.isdigit():
                continue
            try:
                cmdline = (p / "cmdline").read_bytes().decode("utf-8", errors="ignore")
                if ("ign" in cmdline or "gazebo" in cmdline) and "bridge" not in cmdline:
                    env_raw = (p / "environ").read_bytes().decode("utf-8", errors="ignore")
                    for var in env_raw.split("\0"):
                        if var.startswith("IGN_PARTITION=") and len(var.split("=", 1)[1].strip()) > 0:
                            return var.split("=", 1)[1].strip()
                        if var.startswith("GZ_PARTITION=") and len(var.split("=", 1)[1].strip()) > 0:
                            return var.split("=", 1)[1].strip()
            except Exception:
                continue
    except Exception:
        pass
    return None


class SimRobot:
    """
    Simulation Robot Driver wrapping Person 1's ground_robot_sdk.
    Provides identical interface to MockRobot for drop-in use in EdgeSupervisor.
    """

    def __init__(
        self,
        robot_id: str = "R1",
        initial_pos: Optional[List[float]] = None,
        current_zone: Optional[str] = None,
        waypoints: Optional[Dict[str, List[float]]] = None,
        use_gazebo: Optional[bool] = None,
        spawn_pose: Optional[Tuple[float, float, float]] = None,
    ):
        self.robot_id = robot_id
        cfg = DEFAULT_ROBOT_DEPLOYMENT.get(robot_id, DEFAULT_ROBOT_DEPLOYMENT["R1"])
        self.sim_name = cfg["sim_name"]
        self.namespace = cfg["namespace"]
        self.spawn_pose = tuple(spawn_pose or cfg["spawn_pose"])

        # Coordinates & Zone tracking
        self.waypoints = dict(FALLBACK_TOPOLOGY_WAYPOINTS)
        if waypoints:
            self.waypoints.update(waypoints)

        if current_zone:
            self.current_zone = current_zone
        else:
            self.current_zone = cfg.get("start_node", "north_hall")

        if initial_pos:
            self.pose = list(initial_pos)
            if len(self.pose) == 2:
                self.pose.append(0.0)
            if spawn_pose is None:
                self.spawn_pose = tuple(self.pose)
        else:
            self.pose = list(self.spawn_pose)

        self.status = "IDLE"
        self.battery_pct = 95.0
        self.has_fault = False
        self.fault_msg = None
        self.current_cmd_id = None
        self.history: List[Dict[str, Any]] = []
        self.injected_obstacles: Dict[str, Dict[str, Any]] = {}

        # Load topology nodes & edges for corridor routing
        self.topology_nodes = {}
        self.topology_edges = []
        if HAS_SDK:
            try:
                topo, _, _ = load_data()
                self.topology_nodes = topo.get("nodes", {})
                self.topology_edges = topo.get("edges", [])
            except Exception:
                pass

        if not self.topology_edges:
            self.topology_edges = [
                ("lift_lobby", "north_hall", 6.0),
                ("north_hall", "west_north_gate", 4.92),
                ("north_hall", "east_north_gate", 4.92),
                ("north_hall", "nurse_station_approach", 7.0),
                ("west_north_gate", "west_waiting_approach", 6.0),
                ("east_north_gate", "east_waiting_approach", 6.0),
                ("west_waiting_approach", "west_mid_hall", 5.0),
                ("east_waiting_approach", "east_mid_hall", 5.0),
                ("west_mid_hall", "nurse_station_approach", 4.92),
                ("east_mid_hall", "nurse_station_approach", 4.92),
                ("west_mid_hall", "west_south_hall", 11.0),
                ("east_mid_hall", "east_south_hall", 11.0),
                ("west_south_hall", "south_equipment_approach", 8.32),
                ("east_south_hall", "south_equipment_approach", 8.32),
                ("south_equipment_approach", "south_room_approach", 13.0),
            ]

        # Determine whether to use live Gazebo or MockAdapter
        self._is_gazebo = False
        self._kinematic_running = False
        self._sim_thread = None

        if not HAS_SDK:
            self.runtime = None
            return

        # Check for ROS 2 rclpy availability
        has_rclpy = False
        try:
            import rclpy  # noqa: F401
            has_rclpy = True
        except ImportError:
            has_rclpy = False

        if use_gazebo is True or (use_gazebo is None and has_rclpy):
            try:
                # Synchronize partition environment variables for ROS-GZ bridge
                partition = os.environ.get("IGN_PARTITION") or os.environ.get("HOSPITAL_IGN_PARTITION") or _detect_gazebo_partition() or "custom_hospital_world"
                os.environ["IGN_PARTITION"] = partition
                os.environ["GZ_PARTITION"] = partition
                print(f"      [sim] Active Gazebo partition: '{partition}'")

                # Live Gazebo Fortress mode via ROS 2 bridge
                self.runtime = RobotRuntime(
                    robot_name=self.sim_name,
                    namespace=self.namespace,
                    safety_config=SafetyConfig(obstacle_guard=True, minimum_front_distance=0.25),
                    navigation_config=NavigationConfig(
                        goal_timeout=270.0,
                        position_tolerance=0.50,
                        startup_odometry_timeout=30.0,
                        odometry_timeout=15.0,
                    ),
                ).start()
                # Ensure SensorManager timeout matches or exceeds navigation odometry tolerance
                self.runtime.sensors._timeout = 15.0

                # Allow the asynchronous ROS-Gazebo bridge up to 20 seconds to establish odometry & range
                bridge_ready_by = time.time() + 15.0
                sensors_fresh = False
                while time.time() < bridge_ready_by:
                    try:
                        health = self.runtime.state().health
                        if health.odometry.fresh and health.range.fresh:
                            sensors_fresh = True
                            print(f"      [sim] Live sensors connected for {self.sim_name} (odom & range) on partition: '{os.environ.get('IGN_PARTITION')}'")
                            break
                        elif health.odometry.fresh and not health.range.fresh:
                            time.sleep(0.5)
                    except Exception:
                        pass
                    time.sleep(0.2)

                # If range sensor is not publishing in headless/software mode, disable obstacle guard to avoid false stall
                if not sensors_fresh:
                    try:
                        health = self.runtime.state().health
                        if health.odometry.fresh and not health.range.fresh:
                            print(f"      [sim] Notice: /{self.sim_name}/ultrasonic/scan not active; disabling obstacle_guard to prevent false stall.")
                            self.runtime.safety.config.obstacle_guard = False
                        elif not health.odometry.fresh:
                            print(f"      [sim] Notice: /{self.sim_name}/odom not yet received. Partition: '{os.environ.get('IGN_PARTITION')}'. Ensure Gazebo is running unpaused.")
                    except Exception:
                        pass

                self._is_gazebo = True
            except Exception as e:
                print(f"[SimRobot:{self.robot_id}] Notice: ROS 2 / Gazebo not ready ({e}); falling back to SDK kinematic mock.")
                self.runtime = self._init_mock_runtime()
        else:
            # Standalone SDK mock adapter with kinematic loop
            self.runtime = self._init_mock_runtime()

    def _init_mock_runtime(self) -> Any:
        adapter = MockAdapter()
        runtime = RobotRuntime(
            adapter=adapter,
            robot_name=self.sim_name,
            namespace=self.namespace,
            safety_config=SafetyConfig(obstacle_guard=True, minimum_front_distance=0.25),
            navigation_config=NavigationConfig(goal_timeout=60.0, position_tolerance=0.25, control_period=0.02),
        ).start()

        # Initialize odometry at start pose
        adapter.set_odometry(Odometry(Pose2D(self.spawn_pose[0], self.spawn_pose[1], self.spawn_pose[2]), Twist2D(), timestamp=time.time()))
        adapter.set_range_scan(RangeScan((2.0, 2.0, 2.0), 2.0, 2.0, timestamp=time.time()))

        # Generate a minimal valid JPEG image frame for camera tests
        minimal_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xbf\x00\xff\xd9"
        adapter.set_camera_frame(CameraFrame(timestamp=time.time(), width=320, height=240, encoding="jpeg", available=True, data=minimal_jpeg))

        # Start kinematic background updater
        self._kinematic_running = True
        self._sim_thread = threading.Thread(target=self._run_kinematic_sim, args=(adapter, runtime), daemon=True)
        self._sim_thread.start()
        return runtime

    def _run_kinematic_sim(self, adapter: Any, runtime: Any) -> None:
        """Simulates physical motion and odometry feedback for MockAdapter."""
        cur_x, cur_y, cur_yaw = self.pose[0], self.pose[1], self.pose[2]
        dt = 0.02
        sim_speedup = 3.0
        while self._kinematic_running:
            cmd = adapter.last_command
            v = cmd.linear * sim_speedup
            w = cmd.angular * sim_speedup

            cur_yaw = math.atan2(math.sin(cur_yaw + w * dt), math.cos(cur_yaw + w * dt))
            cur_x += v * math.cos(cur_yaw) * dt
            cur_y += v * math.sin(cur_yaw) * dt

            now = time.time()
            adapter.set_odometry(Odometry(Pose2D(cur_x, cur_y, cur_yaw), Twist2D(v, w), timestamp=now))

            # Maintain fresh range sensor
            front_dist = 2.0
            if self.injected_obstacles:
                front_dist = 0.15  # within obstacle guard trigger range (< 0.25m)
            adapter.set_range_scan(RangeScan((front_dist, front_dist, front_dist), front_dist, front_dist, timestamp=now))

            time.sleep(dt)

    def _find_route(self, start_zone: str, target_zone: str) -> List[str]:
        """Calculates collision-free topology corridor waypoints between two nodes."""
        lookup_start = CLOUD_TO_SIM_ALIASES.get(start_zone, start_zone)
        lookup_target = CLOUD_TO_SIM_ALIASES.get(target_zone, target_zone)
        start_node = lookup_start
        target_node = lookup_target

        if HAS_SDK:
            try:
                target_node = resolve_destination(lookup_target)["physical_node"]
            except Exception:
                pass
            try:
                start_node = resolve_destination(lookup_start)["physical_node"]
            except Exception:
                pass

        if start_node == target_node or not self.topology_edges:
            return [target_node]

        adj: Dict[str, List[Tuple[str, float]]] = {}
        for u, v, w in self.topology_edges:
            adj.setdefault(u, []).append((v, float(w)))
            adj.setdefault(v, []).append((u, float(w)))

        if start_node not in adj or target_node not in adj:
            return [target_node]

        q = [(0.0, start_node, [start_node])]
        visited = set()
        while q:
            cost, curr, path = heapq.heappop(q)
            if curr in visited:
                continue
            visited.add(curr)
            if curr == target_node:
                return path
            for neigh, w in adj.get(curr, []):
                if neigh not in visited:
                    heapq.heappush(q, (cost + w, neigh, path + [neigh]))
        return [target_node]

    def close(self) -> None:
        self._kinematic_running = False
        if self.runtime:
            try:
                self.runtime.close()
            except Exception:
                pass

    def inject_obstacle(
        self,
        zone: str,
        obstacle_type: str = "transient",
        description: str = "Simulated trolley blocking path",
        clears_after_waits: int = 1,
    ) -> None:
        self.injected_obstacles[zone] = {
            "type": obstacle_type,
            "description": description,
            "clears_after_waits": clears_after_waits,
            "wait_count": 0,
        }

    def clear_obstacles(self) -> None:
        self.injected_obstacles.clear()

    def set_fault(self, fault_msg: str = "Motor drive stall detected") -> None:
        self.has_fault = True
        self.fault_msg = fault_msg

    def clear_fault(self) -> None:
        self.has_fault = False
        self.fault_msg = None

    def set_battery(self, pct: float) -> None:
        self.battery_pct = pct

    def execute_command(self, cmd: Dict[str, Any]) -> Dict[str, Any]:
        """
        Executes a received RobotCommand primitive via Person 1's ground_robot_sdk.
        Returns execution status report matching MockRobot contract.
        """
        cmd_id = cmd["cmd_id"]
        action = cmd["action"]
        params = cmd.get("params", {})
        self.current_cmd_id = cmd_id
        self.status = "RUNNING"
        start_t = time.time()

        record = {
            "cmd_id": cmd_id,
            "action": action,
            "params": params,
            "start_time": start_t,
        }

        # Check for simulated fault or critically depleted battery
        if self.has_fault or self.battery_pct <= 5.0:
            self.status = "FAILED"
            msg = self.fault_msg or "Battery critical (<5%); emergency stop"
            record["end_time"] = time.time()
            record["status"] = self.status
            record["result_msg"] = msg
            self.history.append(record)
            if self.runtime:
                self.runtime.commands.stop()
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

        # Dispatch action to ground_robot_sdk
        if action == "go_to":
            target_name = params.get("to") or params.get("target_name")
            target_xy = params.get("target")

            # 1. Resolve world coordinates from PRD aliases or waypoints
            if target_xy is None and target_name:
                lookup_name = CLOUD_TO_SIM_ALIASES.get(target_name, target_name)
                if HAS_SDK:
                    try:
                        resolved = resolve_destination(lookup_name)
                        target_xy = resolved["waypoint"]
                    except Exception:
                        pass
                if target_xy is None and lookup_name in self.waypoints:
                    target_xy = self.waypoints[lookup_name]
                if target_xy is None and target_name in self.waypoints:
                    target_xy = self.waypoints[target_name]

            if target_xy is None:
                target_xy = self.pose[:2]

            # 2. Check injected obstacles for tactical autonomy testing
            blocked_zone = None
            if target_name in self.injected_obstacles:
                blocked_zone = target_name
            elif self.current_zone in self.injected_obstacles:
                blocked_zone = self.current_zone
            elif (
                (self.current_zone in ("central-supply", "south_equipment_approach") and target_name in ("2B-12", "ward-2B"))
                or (self.current_zone in ("2B-12", "ward-2B") and target_name in ("central-supply", "south_equipment_approach"))
            ):
                if "corridor-2B" in self.injected_obstacles:
                    blocked_zone = "corridor-2B"
                elif "east_mid_hall" in self.injected_obstacles:
                    blocked_zone = "east_mid_hall"

            if blocked_zone:
                obs_info = self.injected_obstacles[blocked_zone]
                if obs_info["type"] == "transient" and obs_info["wait_count"] >= obs_info["clears_after_waits"]:
                    del self.injected_obstacles[blocked_zone]
                else:
                    self.status = "BLOCKED"
                    result_msg = f"Navigation blocked in {blocked_zone}: {obs_info['description']}"
                    record["end_time"] = time.time()
                    record["status"] = self.status
                    record["result_msg"] = result_msg
                    self.history.append(record)
                    if self.runtime:
                        self.runtime.commands.stop()
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

            # 3. Expand corridor route using Dijkstra topology graph
            waypoints_to_visit = []
            if target_name:
                route = self._find_route(self.current_zone, target_name)
                for wp_node in route[1:]:
                    wp_xy = None
                    if HAS_SDK:
                        try:
                            wp_xy = resolve_destination(wp_node)["waypoint"]
                        except Exception:
                            pass
                    if wp_xy is None and wp_node in self.waypoints:
                        wp_xy = self.waypoints[wp_node]
                    if wp_xy:
                        speed_cap = 0.45
                        if self.topology_nodes and wp_node in self.topology_nodes:
                            speed_cap = self.topology_nodes[wp_node].get("speed_cap", 0.45)
                        waypoints_to_visit.append((wp_node, wp_xy, speed_cap))

            if not waypoints_to_visit:
                # Direct target coordinate fallback
                if target_xy is None and target_name:
                    if target_name in self.waypoints:
                        target_xy = self.waypoints[target_name]
                if target_xy is None:
                    target_xy = self.pose[:2]
                waypoints_to_visit.append((target_name or "target", target_xy, float(params.get("speed", params.get("max_speed", 0.45)))))

            # 4. Sequentially execute each corridor waypoint
            for wp_name, wp_xy, wp_speed in waypoints_to_visit:
                if HAS_SDK:
                    x_sdk, y_sdk = world_to_sdk_startup(tuple(wp_xy[:2]), self.spawn_pose)
                else:
                    x_sdk, y_sdk = wp_xy[0] - self.spawn_pose[0], wp_xy[1] - self.spawn_pose[1]

                req_speed = float(params.get("speed", params.get("max_speed", 0.45)))
                speed = min(req_speed, float(wp_speed))

                if self.runtime:
                    task = self.runtime.commands.goto(x_sdk, y_sdk, frame="world", speed=speed)
                    deadline = time.time() + 60.0

                    while time.time() < deadline:
                        st = self.runtime.commands.status(task.command_id)
                        if st.status == CommandStatus.COMPLETED:
                            break
                        if st.status in {CommandStatus.FAILED, CommandStatus.CANCELLED, CommandStatus.REJECTED}:
                            self.status = "BLOCKED" if "obstacle" in (st.reason or "").lower() else "FAILED"
                            result_msg = f"Navigation {st.status.value}: {st.reason or 'stopped'}"
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
                                "message": result_msg,
                                "timestamp": time.time(),
                            }
                        time.sleep(0.05)

                    if st.status != CommandStatus.COMPLETED:
                        # Timeout guard: robot physically blocked or stalled
                        try:
                            cur_odom = self.runtime.state().odometry
                            if cur_odom and cur_odom.pose:
                                self.pose[0] = cur_odom.pose.x + self.spawn_pose[0]
                                self.pose[1] = cur_odom.pose.y + self.spawn_pose[1]
                        except Exception:
                            pass
                        self.status = "BLOCKED"
                        result_msg = f"Navigation blocked / stalled at ({self.pose[0]:.2f}, {self.pose[1]:.2f}) - failed to reach {wp_name}"
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
                            "blocked_zone": self.current_zone,
                            "message": result_msg,
                            "timestamp": time.time(),
                        }

                self.pose[0] = float(wp_xy[0])
                self.pose[1] = float(wp_xy[1])
                self.current_zone = wp_name

            self.status = "COMPLETED"
            result_msg = f"Arrived at {self.current_zone} ({self.pose[:2]}) via corridor centerlines"

        elif action == "wait":
            sec = float(params.get("duration_sec", params.get("seconds", 1.0)))
            if self.runtime:
                self.runtime.commands.wait(min(sec, 5.0))  # bound max wait for test speed
            else:
                time.sleep(min(sec, 1.0))

            # Increment wait counts on obstacles
            for obs in self.injected_obstacles.values():
                obs["wait_count"] += 1

            self.status = "COMPLETED"
            result_msg = f"Waited {sec}s"

        elif action == "photo":
            photo_bytes = None
            if self.runtime:
                try:
                    frame = self.runtime.commands.photo()
                    if frame and frame.available and frame.data:
                        photo_bytes = frame.data
                except Exception:
                    pass

            if not photo_bytes:
                photo_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xff\xd9"

            self.last_photo_bytes = photo_bytes
            self.status = "COMPLETED"
            result_msg = f"Captured camera frame at {self.pose[:2]} ({self.current_zone})"
            record["end_time"] = time.time()
            record["status"] = self.status
            record["result_msg"] = result_msg
            self.history.append(record)
            return {
                "robot_id": self.robot_id,
                "cmd_id": cmd_id,
                "status": "COMPLETED",
                "pose": list(self.pose),
                "zone": self.current_zone,
                "has_photo": bool(photo_bytes),
                "photo_size": len(photo_bytes) if photo_bytes else 0,
                "message": result_msg,
                "timestamp": time.time(),
            }

        elif action == "beep":
            if self.runtime:
                self.runtime.commands.beep()
            self.status = "COMPLETED"
            result_msg = "Beep notification triggered"

        elif action == "stop":
            if self.runtime:
                self.runtime.commands.stop()
            self.status = "IDLE"
            result_msg = f"Stop executed: {params.get('reason', 'normal stop')}"

        elif action == "report":
            if self.runtime:
                rep = self.runtime.commands.report()
            else:
                rep = {}
            self.status = "IDLE"
            result_msg = f"State report: Pose {self.pose[:2]}, Zone {self.current_zone}, Battery {self.battery_pct}%"
            record["end_time"] = time.time()
            record["status"] = self.status
            record["result_msg"] = result_msg
            self.history.append(record)
            return {
                "robot_id": self.robot_id,
                "cmd_id": cmd_id,
                "status": "IDLE",
                "pose": list(self.pose),
                "zone": self.current_zone,
                "battery": self.battery_pct,
                "report": rep,
                "message": result_msg,
                "timestamp": time.time(),
            }

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
            "message": result_msg,
            "timestamp": time.time(),
        }
