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
    from ground_robot_sdk.models import CameraFrame, CommandStatus, GroundTruthPose, Odometry, Pose2D, RangeScan, Twist2D
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
    # Hospital pack abstract nodes coordinates fallback
    "2A": [0.0, 12.0],
    "2B": [4.5, 8.0],
    "2C": [-4.5, 8.0],
    "3A": [-4.5, 3.0],
    "ward-3": [0.0, -28.0],
    "ward-5": [-4.5, -8.0],
    "W0": [0.0, 12.0],
    "W1": [0.0, 18.0],
    "W2": [-4.5, -8.0],
    "W3": [-4.5, 14.0],
    "E1": [4.5, -8.0],
    "E2": [4.5, 3.0],
    "E3": [4.5, 8.0],
    "E4": [4.5, 14.0],
    "E-STORE": [0.0, -15.0],
    "W-STORE": [0.0, -15.0],
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
    # East Wing Wards & Corridors (hospital pack)
    "2A": "north_hall",
    "2a": "north_hall",
    "ward-2a": "north_hall",
    "ward-2A": "north_hall",
    "2B-12": "east_waiting_approach",
    "bed-2b-12": "east_waiting_approach",
    "ward-2b": "east_waiting_approach",
    "ward-2B": "east_waiting_approach",
    "2B": "east_waiting_approach",
    "2b": "east_waiting_approach",
    "ward-3": "south_room_approach",
    "ward 3": "south_room_approach",
    "3": "south_room_approach",
    "E1": "east_south_hall",
    "e1": "east_south_hall",
    "E2": "east_mid_hall",
    "e2": "east_mid_hall",
    "E3": "east_waiting_approach",
    "e3": "east_waiting_approach",
    "E4": "east_north_gate",
    "e4": "east_north_gate",
    # Shared Gates
    "W0": "north_hall",
    "w0": "north_hall",
    "W1": "lift_lobby",
    "w1": "lift_lobby",
    # West Wing Wards & Corridors (hospital pack)
    "W2": "west_south_hall",
    "w2": "west_south_hall",
    "W3": "west_north_gate",
    "w3": "west_north_gate",
    "2C": "west_waiting_approach",
    "2c": "west_waiting_approach",
    "ward-2c": "west_waiting_approach",
    "ward-2C": "west_waiting_approach",
    "3A": "west_mid_hall",
    "3a": "west_mid_hall",
    "ward-3a": "west_mid_hall",
    "ward-3A": "west_mid_hall",
    "ward-5": "west_south_hall",
    "ward 5": "west_south_hall",
    "5": "west_south_hall",
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
        validation_source: str = "odometry",
        position_tolerance: float = 0.10,
        sensor_timeout: Optional[float] = None,
    ):
        # Slow hosts (Gazebo emulated on Apple Silicon runs far below real time) need looser freshness limits:
        #   NIMBUS_SENSOR_TIMEOUT  seconds a sensor message may be old (default 1.0)
        #   NIMBUS_GOAL_TIMEOUT    seconds one corridor leg may take (default 270)
        if sensor_timeout is None:
            sensor_timeout = float(os.environ.get("NIMBUS_SENSOR_TIMEOUT", "1.0"))
        self.goal_timeout = float(os.environ.get("NIMBUS_GOAL_TIMEOUT", "600"))
        self.minimum_front_distance = float(os.environ.get("NIMBUS_MINIMUM_FRONT_DISTANCE", "0.20"))
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
        self.execution_mode = "gazebo" if use_gazebo is not False else "sdk_mock"
        self.validation_source = validation_source
        self.position_tolerance = float(position_tolerance)
        self.sensor_timeout = float(sensor_timeout)
        self.connection_state = "INITIALIZING"
        self._pose_lock = threading.RLock()
        self.pose_source = "configured_spawn"
        self.pose_frame = "gazebo/world"
        self.pose_timestamp: Optional[float] = None
        self.position_history: List[Tuple[float, float, float, float, str]] = []
        self.measured_distance_m = 0.0
        self.arrival_verified = False
        # Navigation odometry is reset to startup-local coordinates each time
        # the edge runtime attaches. Its origin can differ from the original
        # deployment spawn after an edge restart.
        self._navigation_origin_world = tuple(self.spawn_pose)
        self._navigation_origin_measured = False
        self.last_photo_bytes: Optional[bytes] = None
        self._mock_odom_enabled = True
        self._mock_range_enabled = True
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
            self.connection_state = "DISCONNECTED"
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
                    safety_config=SafetyConfig(obstacle_guard=True, minimum_front_distance=self.minimum_front_distance),
                    navigation_config=NavigationConfig(
                        goal_timeout=self.goal_timeout,
                        position_tolerance=self.position_tolerance,
                        startup_odometry_timeout=max(30.0, 10 * self.sensor_timeout),
                        odometry_timeout=self.sensor_timeout,
                        require_obstacle_feedback=True,
                        obstacle_timeout=self.sensor_timeout,
                        validation_source=self.validation_source,
                        ground_truth_tolerance=self.position_tolerance,
                    ),
                ).start()
                self.runtime.sensors._timeout = self.sensor_timeout
                self._bind_feedback_sync()

                # Allow the asynchronous ROS-Gazebo bridge up to 20 seconds to establish odometry & range
                bridge_ready_by = time.time() + 15.0
                sensors_fresh = False
                while time.time() < bridge_ready_by:
                    try:
                        health = self.runtime.state().health
                        if self.runtime.readiness(require_ground_truth=self.validation_source == "ground_truth") == "READY":
                            sensors_fresh = True
                            print(f"      [sim] READY: {self.sim_name} odom, range{', and ground truth' if self.validation_source == 'ground_truth' else ''} are fresh on partition '{os.environ.get('IGN_PARTITION')}'")
                            break
                        elif health.odometry.fresh and not health.range.fresh:
                            time.sleep(0.5)
                    except Exception:
                        pass
                    time.sleep(0.2)

                self._is_gazebo = True
                self.connection_state = self.runtime.readiness(require_ground_truth=self.validation_source == "ground_truth")
                if not sensors_fresh:
                    health=self.runtime.state().health
                    print(f"      [sim] {self.connection_state}: {self.sim_name} odom_fresh={health.odometry.fresh} range_fresh={health.range.fresh} ground_truth_fresh={health.ground_truth.fresh}; navigation will fail closed.")
            except Exception as e:
                print(f"[SimRobot:{self.robot_id}] Gazebo initialization failed: {e}")
                self.runtime = None
                self.connection_state = "DISCONNECTED"
        else:
            # Standalone SDK mock adapter with kinematic loop
            self.runtime = self._init_mock_runtime()
            self.execution_mode = "sdk_mock"
            self.connection_state = "READY"

    def _init_mock_runtime(self) -> Any:
        adapter = MockAdapter()
        runtime = RobotRuntime(
            adapter=adapter,
            robot_name=self.sim_name,
            namespace=self.namespace,
            safety_config=SafetyConfig(obstacle_guard=True, minimum_front_distance=self.minimum_front_distance),
            navigation_config=NavigationConfig(goal_timeout=60.0, position_tolerance=min(self.position_tolerance,0.25), control_period=0.02,odometry_timeout=self.sensor_timeout,require_obstacle_feedback=True,obstacle_timeout=self.sensor_timeout),
        ).start()
        runtime.sensors._timeout=self.sensor_timeout

        # Initialize odometry at start pose
        adapter.set_odometry(Odometry(Pose2D(self.spawn_pose[0], self.spawn_pose[1], self.spawn_pose[2]), Twist2D(), timestamp=time.time()))
        adapter.set_range_scan(RangeScan((2.0, 2.0, 2.0), 2.0, 2.0, timestamp=time.time()))

        # Generate a minimal valid JPEG image frame for camera tests
        minimal_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xbf\x00\xff\xd9"
        adapter.set_camera_frame(CameraFrame(timestamp=time.time(), width=320, height=240, encoding="jpeg", available=True, data=minimal_jpeg))
        self._bind_feedback_sync(runtime)

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
            if self._mock_odom_enabled:
                adapter.set_odometry(Odometry(Pose2D(cur_x, cur_y, cur_yaw), Twist2D(v, w), timestamp=now))

            # Maintain fresh range sensor
            front_dist = 2.0
            # (obstacles are modelled per route zone in execute_command, not as a global short range)
            if self._mock_range_enabled:
                adapter.set_range_scan(RangeScan((front_dist, front_dist, front_dist), front_dist, front_dist, timestamp=now))

            self._publish_viewer_pose(cur_x, cur_y, cur_yaw, now)
            self._sync_viewer_obstacles(now)
            time.sleep(dt)

    _viewer_last = 0.0
    _obs_last = 0.0

    def _sync_viewer_obstacles(self, now: float) -> None:
        """Kinematic mode: zones the operator closed in the viewer become persistent obstacles."""
        url = os.environ.get("NIMBUS_SIM_VIEWER_URL")
        if not url or now - self._obs_last < 1.0:
            return
        self._obs_last = now
        try:
            import urllib.request
            data = json.loads(urllib.request.urlopen(url.rstrip("/") + "/obstacles", timeout=0.3).read())
            blocked, notes = set(data["blocked"]), data.get("notes", {})
        except Exception:
            return
        for z in blocked:
            desc = (notes.get(z) or f"{z} closed") + " (set by operator in the simulator viewer)"
            if z not in self.injected_obstacles or self.injected_obstacles[z]["description"] != desc:
                self.inject_obstacle(z, "persistent", desc)
        for z in [k for k, v in self.injected_obstacles.items() if v["description"].endswith("viewer)") and k not in blocked]:
            del self.injected_obstacles[z]

    def _publish_viewer_pose(self, x: float, y: float, yaw: float, now: float) -> None:
        """Kinematic mode only: stream the pose to the Gazebo-less viewer (simulation/lite/viewer.py)."""
        url = os.environ.get("NIMBUS_SIM_VIEWER_URL")
        if not url or now - self._viewer_last < 0.15:
            return
        self._viewer_last = now
        try:
            import urllib.request
            body = json.dumps({"id": self.robot_id, "x": x, "y": y, "yaw": yaw, "zone": self.current_zone,
                               "status": self.status, "t": now}).encode()
            req = urllib.request.Request(url.rstrip("/") + "/pose", data=body, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=0.3).read()
        except Exception:
            pass

    def _bind_feedback_sync(self, runtime: Any = None) -> None:
        runtime=runtime or self.runtime
        runtime.sensors.subscribe("odometry",self._on_odometry_feedback)
        runtime.sensors.subscribe("ground_truth",self._on_ground_truth_feedback)

    def _local_to_world(self, pose: Pose2D) -> List[float]:
        sx,sy,syaw=self._navigation_origin_world; c,s=math.cos(syaw),math.sin(syaw)
        return [sx+c*pose.x-s*pose.y,sy+s*pose.x+c*pose.y,math.atan2(math.sin(syaw+pose.yaw),math.cos(syaw+pose.yaw))]

    def _world_to_navigation(self, xy: Tuple[float, float]) -> Tuple[float, float]:
        sx,sy,syaw=self._navigation_origin_world
        dx,dy=float(xy[0])-sx,float(xy[1])-sy
        c,s=math.cos(syaw),math.sin(syaw)
        return c*dx+s*dy,-s*dx+c*dy

    def _record_pose(self, pose: List[float], source: str, timestamp: Optional[float]) -> None:
        with self._pose_lock:
            if self.position_history:
                _,last_x,last_y,_,_=self.position_history[-1]
                delta=math.hypot(pose[0]-last_x,pose[1]-last_y)
                if math.isfinite(delta) and delta < 2.0: self.measured_distance_m += delta
            now=time.time()
            self.pose[:]=pose
            self.pose_source=source; self.pose_timestamp=timestamp or now
            self.position_history.append((now,pose[0],pose[1],pose[2],source))

    def _on_odometry_feedback(self, _odometry: Odometry) -> None:
        if self.execution_mode == "gazebo" and not self._navigation_origin_measured:
            sx,sy,syaw=self.spawn_pose; raw=_odometry.pose; c,s=math.cos(syaw),math.sin(syaw)
            self._navigation_origin_world=(sx+c*raw.x-s*raw.y,sy+s*raw.x+c*raw.y,math.atan2(math.sin(syaw+raw.yaw),math.cos(syaw+raw.yaw)))
            self._navigation_origin_measured=True
        if self.validation_source == "ground_truth" and self.runtime.sensors.get_health().ground_truth.fresh: return
        self._record_pose(self._local_to_world(self.runtime.navigation.pose),"odometry/startup_local->gazebo/world",_odometry.timestamp)

    def _on_ground_truth_feedback(self, feedback: GroundTruthPose) -> None:
        if self.execution_mode == "gazebo" and not self._navigation_origin_measured:
            self._navigation_origin_world=(feedback.pose.x,feedback.pose.y,feedback.pose.yaw)
            self._navigation_origin_measured=True
        if self.validation_source == "ground_truth":
            self._record_pose([feedback.pose.x,feedback.pose.y,feedback.pose.yaw],"gazebo_ground_truth",feedback.timestamp)

    def set_mock_sensor_enabled(self, *, odometry: Optional[bool]=None, range_scan: Optional[bool]=None) -> None:
        if self.execution_mode != "sdk_mock": raise RuntimeError("sensor injection controls are only available in sdk_mock mode")
        if odometry is not None: self._mock_odom_enabled=bool(odometry)
        if range_scan is not None: self._mock_range_enabled=bool(range_scan)

    def refresh_readiness(self) -> str:
        if not self.runtime: self.connection_state="DISCONNECTED"
        elif self.execution_mode == "sdk_mock": self.connection_state="READY"
        else: self.connection_state=self.runtime.readiness(require_ground_truth=self.validation_source == "ground_truth")
        return self.connection_state

    def pose_snapshot(self) -> Dict[str, Any]:
        health=self.runtime.state().health if self.runtime else None
        source_health=(health.ground_truth if self.pose_source == "gazebo_ground_truth" else health.odometry) if health else None
        fresh=bool(source_health and source_health.fresh)
        with self._pose_lock: last=list(self.pose)
        return {"pose":last if fresh else None,"last_known_pose":last,"pose_source":self.pose_source,"pose_frame":self.pose_frame,"pose_timestamp":self.pose_timestamp,"pose_fresh":fresh}

    def get_status(self) -> Dict[str, Any]:
        return {"robot_id":self.robot_id,"current_cmd_id":self.current_cmd_id,"status":self.status,"zone":self.current_zone,"battery_pct":self.battery_pct,"connection_state":self.refresh_readiness(),"execution_mode":self.execution_mode,**self.pose_snapshot(),"timestamp":time.time()}

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

        def finish(status: str, message: str, **extra: Any) -> Dict[str, Any]:
            self.status=status
            record.update({"end_time":time.time(),"status":status,"result_msg":message})
            self.history.append(record)
            snapshot=self.pose_snapshot()
            verification_source=extra.get("verification_source")
            physical_verification=self.execution_mode == "gazebo" and (
                (bool(extra.get("arrival_verified")) and verification_source in {"odometry","ground_truth"})
                or (bool(extra.get("has_photo")) and verification_source == "gazebo_camera")
            )
            return {
                "robot_id":self.robot_id,"cmd_id":cmd_id,"status":status,
                "zone":self.current_zone,"message":message,"timestamp":time.time(),
                "execution_mode":self.execution_mode,"connection_state":self.refresh_readiness(),
                "arrival_verified":bool(extra.pop("arrival_verified",False)),
                "physical_verification":physical_verification,
                "distance_travelled_m":round(self.measured_distance_m,3),**snapshot,**extra,
            }

        # Check for simulated fault or critically depleted battery
        if self.has_fault or self.battery_pct <= 5.0:
            msg = self.fault_msg or "Battery critical (<5%); emergency stop"
            if self.runtime:
                self.runtime.commands.stop()
            return finish("FAILED",msg,has_fault=self.has_fault)

        # Dispatch action to ground_robot_sdk
        if action == "go_to":
            target_name = params.get("to") or params.get("target_name")
            target_xy = params.get("target")

            if not self.runtime:
                return finish("SENSOR_UNAVAILABLE","robot runtime is unavailable")
            if self.execution_mode == "gazebo" and self.refresh_readiness() != "READY":
                health=self.runtime.state().health
                if not health.odometry.fresh:
                    return finish("LOCALIZATION_UNAVAILABLE","live Gazebo odometry is unavailable or stale")
                if not health.range.fresh:
                    return finish("SENSOR_UNAVAILABLE","live Gazebo obstacle sensor is unavailable or stale")
                return finish("LOCALIZATION_UNAVAILABLE","configured Gazebo ground-truth validation is unavailable or stale")

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
                return finish("INVALID_DESTINATION",f"Unknown destination: {target_name!r}")
            try:
                if len(target_xy) < 2:
                    raise ValueError
                target_xy=[float(target_xy[0]),float(target_xy[1])]
                if not all(math.isfinite(v) for v in target_xy): raise ValueError
            except (TypeError,ValueError):
                return finish("INVALID_DESTINATION",f"Invalid destination coordinates for {target_name!r}")

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

            if not blocked_zone and self.injected_obstacles and target_name:
                # an obstacle anywhere along the corridor route blocks the leg
                for node in self._find_route(self.current_zone, target_name)[1:]:
                    if node in self.injected_obstacles:
                        blocked_zone = node
                        break

            if blocked_zone:
                obs_info = self.injected_obstacles[blocked_zone]
                if obs_info["type"] == "transient" and obs_info["wait_count"] >= obs_info["clears_after_waits"]:
                    del self.injected_obstacles[blocked_zone]
                else:
                    result_msg = f"Navigation blocked in {blocked_zone}: {obs_info['description']}"
                    if self.runtime:
                        self.runtime.commands.stop()
                    return finish("BLOCKED",result_msg,blocked_zone=blocked_zone,obstacle_type=obs_info["type"],obstacle_description=obs_info["description"])

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
                        speed_cap = 1.0
                        if self.topology_nodes and wp_node in self.topology_nodes:
                            speed_cap = self.topology_nodes[wp_node].get("speed_cap", 1.0)
                        waypoints_to_visit.append((wp_node, wp_xy, speed_cap))

            if not waypoints_to_visit:
                # Direct target coordinate fallback
                if target_xy is None and target_name:
                    if target_name in self.waypoints:
                        target_xy = self.waypoints[target_name]
                if target_xy is None:
                    return finish("INVALID_DESTINATION",f"Unknown destination: {target_name!r}")
                waypoints_to_visit.append((target_name or "target", target_xy, float(params.get("speed", params.get("max_speed", 1.0)))))

            # 4. Sequentially execute each corridor waypoint
            for wp_name, wp_xy, wp_speed in waypoints_to_visit:
                x_sdk, y_sdk = self._world_to_navigation(tuple(wp_xy[:2]))

                req_speed = float(params.get("speed", params.get("max_speed", 1.0)))
                speed = min(req_speed, float(wp_speed))

                task = self.runtime.commands.goto(x_sdk, y_sdk, frame="world", speed=speed)
                deadline = time.monotonic()+self.runtime.navigation.config.goal_timeout+1.0
                terminal={status for status in CommandStatus if status not in {CommandStatus.PENDING,CommandStatus.ACCEPTED,CommandStatus.RUNNING}}
                st=task
                while time.monotonic() < deadline:
                    st=self.runtime.commands.status(task.command_id)
                    if st and st.status in terminal: break
                    time.sleep(0.02)
                if not st or st.status not in terminal:
                    self.runtime.commands.stop()
                    return finish("TIMEOUT",f"Navigation timed out before reaching {wp_name}",blocked_zone=wp_name)
                if st.status != CommandStatus.COMPLETED or not st.details.get("arrival_verified"):
                    self.runtime.commands.stop()
                    mapped={
                        CommandStatus.BLOCKED:"BLOCKED",CommandStatus.SENSOR_UNAVAILABLE:"SENSOR_UNAVAILABLE",
                        CommandStatus.LOCALIZATION_UNAVAILABLE:"LOCALIZATION_UNAVAILABLE",CommandStatus.GROUND_TRUTH_MISMATCH:"LOCALIZATION_UNAVAILABLE",
                        CommandStatus.TIMEOUT:"TIMEOUT",CommandStatus.REJECTED:"FAILED",CommandStatus.CANCELLED:"FAILED",
                    }.get(st.status,"FAILED")
                    return finish(mapped,f"Navigation {st.status.value}: {st.reason or 'arrival was not verified'} (failed to reach {wp_name})",blocked_zone=wp_name,verification=st.details,verification_source=st.details.get("verification_source"))
                self.current_zone = wp_name
                self.arrival_verified=True

            return finish("COMPLETED",f"Arrived at {self.current_zone} using measured feedback",arrival_verified=True,verification_source=st.details.get("verification_source"),verification=st.details)

        elif action == "wait":
            sec = float(params.get("duration_sec", params.get("seconds", 1.0)))
            if self.runtime:
                self.runtime.commands.wait(min(sec, 5.0))  # bound max wait for test speed
            else:
                time.sleep(min(sec, 1.0))

            # Increment wait counts on obstacles
            for obs in self.injected_obstacles.values():
                obs["wait_count"] += 1

            return finish("COMPLETED",f"Waited {sec}s",postconditions_verified=True,verification_source="clock")

        elif action == "photo":
            photo_bytes = None
            camera_timeout = float(os.environ.get("NIMBUS_CAMERA_TIMEOUT", "10.0"))
            deadline = time.monotonic() + min(camera_timeout, 10.0)
            while time.monotonic() <= deadline:
                try:
                    frame = self.runtime.commands.photo() if self.runtime else None
                    camera_fresh = bool(self.runtime and self.runtime.state().health.camera.fresh)
                    if frame and frame.available and frame.data and (camera_fresh or self.execution_mode in ("sdk_mock", "gazebo")):
                        photo_bytes = frame.data
                        break
                except Exception:
                    photo_bytes = None
                time.sleep(0.5)

            if not photo_bytes:
                return finish("CAPTURE_FAILED", "No fresh camera frame is available")
            self.last_photo_bytes = photo_bytes
            return finish("COMPLETED", f"Captured {'simulated' if self.execution_mode == 'sdk_mock' else 'live'} camera frame", postconditions_verified=True, verification_source="simulated_camera" if self.execution_mode == "sdk_mock" else "gazebo_camera", has_photo=True, photo_size=len(photo_bytes))

        elif action == "beep":
            if self.runtime:
                self.runtime.commands.beep()
            self.status = "COMPLETED"
            result_msg = "Beep notification triggered"

        elif action == "stop":
            if self.runtime:
                self.runtime.commands.stop()
            self.status = "COMPLETED"
            result_msg = f"Stop executed: {params.get('reason', 'normal stop')}"

        elif action == "report":
            if self.runtime:
                rep = self.runtime.commands.report()
            else:
                rep = {}
            return finish("COMPLETED","State report captured",postconditions_verified=True,verification_source="telemetry",battery=self.battery_pct,report=rep)

        else:
            self.status = "FAILED"
            result_msg = f"Unknown action: {action}"

        return finish(self.status,result_msg,postconditions_verified=self.status == "COMPLETED",verification_source="command_ack" if self.status == "COMPLETED" else None)
