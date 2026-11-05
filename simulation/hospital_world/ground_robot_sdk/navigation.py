"""Runtime-local point-to-point tasks built on the motion and safety SDK."""
from __future__ import annotations

import math
import os
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass

from .models import CommandResult, CommandStatus, GroundTruthPose, Odometry, Pose2D
from .avoidance import AvoidanceConfig, DynamicObstacleAvoider
from .planning import AStarPlanner, OccupancyGrid, path_length


def _angle(value: float) -> float:
    return math.atan2(math.sin(value), math.cos(value))


@dataclass(frozen=True)
class NavigationConfig:
    position_tolerance: float = 0.10
    cruise_speed: float = 1.0
    control_period: float = 0.05
    # Damped and rate-limited steering: the vehicle must not zig-zag across
    # a narrow corridor after a planner turn.
    steering_gain: float = 0.85
    steering_derivative_gain: float = 0.12
    max_steering_rate: float = 1.50
    curvature_slowdown: float = 2.0
    min_turn_speed: float = 0.08
    startup_odometry_timeout: float = 5.0
    odometry_timeout: float = 1.0
    goal_timeout: float = float(os.environ.get("NIMBUS_GOAL_TIMEOUT", "600.0"))
    require_obstacle_feedback: bool = False
    obstacle_timeout: float = 1.0
    validation_source: str = "odometry"
    ground_truth_tolerance: float = 0.25
    max_localization_discrepancy: float = 0.50
    enable_path_planning: bool = False
    enable_dynamic_avoidance: bool = True
    path_lookahead: float = 0.40
    replan_interval: float = 0.35
    dynamic_obstacle_ttl: float = 2.0
    obstacle_wait_timeout: float = 3.0
    avoidance_distance: float = 1.50
    obstacle_inflation: float = 0.85
    minimum_control_speed: float = 0.10
    minimum_alignment_scale: float = 0.30
    approach_speed_gain: float = 0.35
    reverse_required_angle: float = 2.50
    heading_correction_gain: float = 0.60


class NavigationController:
    """Odometry-localised, Ackermann point-to-point controller.

    The first received odometry pose is the runtime's ``(0, 0, 0)`` startup
    frame. A goal waits for that first pose rather than commanding blind
    motion. It fails and stops if odometry becomes stale or the configurable
    goal timeout expires.
    """

    def __init__(self, motion, sensors, limits, config: NavigationConfig | None = None, history_size=128):
        self.motion = motion
        self.sensors = sensors
        self.limits = limits
        self.config = config or NavigationConfig()
        self._lock = threading.RLock()
        self._startup: Pose2D | None = None
        self._pose = Pose2D()
        self._ground_truth_startup: Pose2D | None = None
        self._ground_truth_pose: Pose2D | None = None
        self._active: CommandResult | None = None
        self._last: CommandResult | None = None
        self._target: Pose2D | None = None
        self._costmap: OccupancyGrid | None = None
        self._planner: AStarPlanner | None = None
        self._path: list[tuple[float,float]] = []
        self._path_index = 0
        self._dynamic_cells: dict[tuple[int,int],float] = {}
        self._last_replan = 0.0
        self._blocked_since: float | None = None
        self._replans = 0
        self._avoidance_events = 0
        safety_distance=getattr(getattr(getattr(motion,"safety",None),"config",None),"minimum_front_distance",.25)
        self._avoider=DynamicObstacleAvoider(AvoidanceConfig(
            safety_distance=safety_distance,
            caution_distance=self.config.avoidance_distance,
            obstacle_inflation=self.config.obstacle_inflation,
        ))
        self._cancel = threading.Event()
        self._threads: set[threading.Thread] = set()
        self._closed = False
        self._history = deque(maxlen=history_size)
        self._by_id = {}
        self._unsubscribe = sensors.subscribe("odometry", self._on_odometry)
        self._unsubscribe_ground_truth = sensors.subscribe("ground_truth", self._on_ground_truth)

    @property
    def pose(self) -> Pose2D:
        with self._lock:
            return self._pose

    @property
    def target(self) -> Pose2D | None:
        with self._lock:
            return self._target

    @property
    def ground_truth_pose(self) -> Pose2D | None:
        with self._lock:
            return self._ground_truth_pose

    @property
    def localization_discrepancy(self) -> float | None:
        with self._lock:
            if self._ground_truth_pose is None:
                return None
            return math.hypot(self._pose.x-self._ground_truth_pose.x,self._pose.y-self._ground_truth_pose.y)

    @property
    def path(self) -> tuple[tuple[float,float], ...]:
        with self._lock: return tuple(self._path)

    def configure_costmap(self, costmap: OccupancyGrid | None) -> None:
        """Install a startup-local costmap without changing the command API."""
        with self._lock:
            if self._active and self._active.status == CommandStatus.RUNNING:
                raise RuntimeError("cannot replace the costmap during active navigation")
            self._costmap=costmap.copy() if costmap is not None else None
            self._planner=AStarPlanner(self._costmap) if self._costmap is not None else None
            self._path=[]; self._path_index=0; self._dynamic_cells.clear()

    def goto(self, x: float, y: float, frame="world", speed: float | None = None) -> CommandResult:
        try:
            x, y = float(x), float(y)
            speed = self.config.cruise_speed if speed is None else float(speed)
        except (TypeError, ValueError):
            return self._result("goto", CommandStatus.REJECTED, "x, y, and speed must be numbers")
        if not (math.isfinite(x) and math.isfinite(y)) or frame not in {"world", "base_link"}:
            return self._result("goto", CommandStatus.REJECTED, "frame must be 'world' or 'base_link', and coordinates must be finite")
        if not math.isfinite(speed) or speed <= 0:
            return self._result("goto", CommandStatus.REJECTED, "speed must be a finite positive value")

        speed = min(speed, self.limits.max_linear_velocity)
        with self._lock:
            if self._closed:
                return self._result("goto", CommandStatus.REJECTED, "navigation controller is closed")
            self._cancel_active_locked("replaced by a newer task")
            current = self._pose
            target = (
                Pose2D(
                    current.x + math.cos(current.yaw) * x - math.sin(current.yaw) * y,
                    current.y + math.sin(current.yaw) * x + math.cos(current.yaw) * y,
                )
                if frame == "base_link"
                else Pose2D(x, y)
            )
            if self.config.enable_path_planning and self._planner:
                if not self._costmap.contains_point((target.x,target.y)):
                    return self._result("goto",CommandStatus.INVALID_DESTINATION,"goal is outside the configured navigation costmap")
                if self._costmap.is_occupied(self._costmap.world_to_cell(target.x,target.y)):
                    return self._result("goto",CommandStatus.INVALID_DESTINATION,"goal lies outside the configured traversable corridors")
                path=self._planner.plan((current.x,current.y),(target.x,target.y))
                if not path:
                    return self._result("goto",CommandStatus.NO_PATH,"A* found no traversable path to the goal")
                self._path=list(path); self._path_index=1 if len(path)>1 else 0
            else:
                self._path=[(current.x,current.y),(target.x,target.y)]; self._path_index=1
            result = self._result("goto", CommandStatus.RUNNING)
            result.details.update({"planner":"astar_costmap" if self.config.enable_path_planning and self._planner else "direct","planned_path":[{"x":p[0],"y":p[1]} for p in self._path],"planned_distance_m":path_length(self._path)})
            result.start_time = time.time()
            self._active = result
            self._target = target
            self._dynamic_cells.clear(); self._blocked_since=None; self._replans=0; self._avoidance_events=0; self._last_replan=0.
            self._cancel = threading.Event()
            thread = threading.Thread(
                target=self._run_goto,
                args=(result.command_id, self._cancel, speed),
                daemon=True,
                name=f"ground-robot-goto-{result.command_id[:8]}",
            )
            self._threads.add(thread)
            thread.start()
            return result

    def cancel(self) -> CommandResult | None:
        with self._lock:
            result = self._cancel_active_locked("cancelled")
        if result:
            self.motion.stop(source="autonomy")
        return result

    def stop(self) -> CommandResult:
        with self._lock:
            self._cancel_active_locked("stopped")
        return self.motion.stop(source="autonomy")

    def status(self, command_id=None):
        with self._lock:
            return self._by_id.get(command_id) if command_id else (self._active or self._last)

    def wait(self, seconds: float) -> CommandResult:
        try:
            seconds = float(seconds)
        except (TypeError, ValueError):
            return self._result("wait", CommandStatus.REJECTED, "seconds must be a number")
        if seconds < 0 or not math.isfinite(seconds):
            return self._result("wait", CommandStatus.REJECTED, "seconds must be finite and non-negative")
        result = self._result("wait", CommandStatus.RUNNING)
        result.start_time = time.time()
        time.sleep(seconds)
        result.status = CommandStatus.COMPLETED
        result.completion_time = time.time()
        return result

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            active = self._cancel_active_locked("runtime closed")
            threads = list(self._threads)
        if active:
            self.motion.stop(source="autonomy")
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(timeout=max(1.0, self.config.control_period * 4))
        self._unsubscribe()
        self._unsubscribe_ground_truth()

    def _on_odometry(self, odometry: Odometry) -> None:
        raw = odometry.pose
        with self._lock:
            if self._startup is None:
                self._startup = raw
                self._pose = Pose2D()
                return
            dx, dy = raw.x - self._startup.x, raw.y - self._startup.y
            cosine, sine = math.cos(self._startup.yaw), math.sin(self._startup.yaw)
            self._pose = Pose2D(
                cosine * dx + sine * dy,
                -sine * dx + cosine * dy,
                _angle(raw.yaw - self._startup.yaw),
            )

    def _on_ground_truth(self, feedback: GroundTruthPose) -> None:
        raw = feedback.pose
        with self._lock:
            if self._ground_truth_startup is None:
                self._ground_truth_startup = raw
                self._ground_truth_pose = Pose2D()
                return
            dx, dy = raw.x-self._ground_truth_startup.x, raw.y-self._ground_truth_startup.y
            cosine, sine = math.cos(self._ground_truth_startup.yaw), math.sin(self._ground_truth_startup.yaw)
            self._ground_truth_pose = Pose2D(
                cosine*dx+sine*dy,
                -sine*dx+cosine*dy,
                _angle(raw.yaw-self._ground_truth_startup.yaw),
            )

    def _active_dynamic_cells(self, now: float) -> set[tuple[int,int]]:
        with self._lock:
            expired=[cell for cell,deadline in self._dynamic_cells.items() if deadline <= now]
            for cell in expired: self._dynamic_cells.pop(cell,None)
            return set(self._dynamic_cells)

    def _record_dynamic_obstacles(self, points: tuple[tuple[float,float],...], now: float) -> bool:
        with self._lock:
            if not self._costmap or not points: return False
            cells=self._costmap.inflated_cells(points,self.config.obstacle_inflation)
            centers={self._costmap.world_to_cell(*point) for point in points}
            deadline=now+self.config.dynamic_obstacle_ttl
            # Range noise moves the inflation boundary by a cell. Replan only
            # when the measured obstacle centre enters genuinely new space.
            changed=not self._dynamic_cells or any(cell not in self._dynamic_cells for cell in centers)
            for cell in cells: self._dynamic_cells[cell]=deadline
            return changed

    def _replan(self, pose: Pose2D, target: Pose2D, now: float, force: bool=False) -> bool:
        with self._lock:
            planner=self._planner
            if not planner: return True
            if not force and now-self._last_replan < self.config.replan_interval: return bool(self._path)
            self._last_replan=now
        dynamic=self._active_dynamic_cells(now)
        path=planner.plan((pose.x,pose.y),(target.x,target.y),dynamic)
        if not path: return False
        with self._lock:
            self._path=list(path); self._path_index=1 if len(path)>1 else 0; self._replans+=1
            active=self._active
            if active:
                active.details.update({"planned_path":[{"x":p[0],"y":p[1]} for p in path],"planned_distance_m":path_length(path),"replans":self._replans})
        return True

    def _tracking_point(self, pose: Pose2D, target: Pose2D) -> tuple[float,float]:
        with self._lock:
            if not self._path: return target.x,target.y
            start=max(0,self._path_index-2)
            closest=min(range(start,len(self._path)),key=lambda index:math.hypot(self._path[index][0]-pose.x,self._path[index][1]-pose.y))
            self._path_index=max(self._path_index,closest)
            travelled=0.; lookahead_index=self._path_index
            while lookahead_index < len(self._path)-1 and travelled < self.config.path_lookahead:
                left,right=self._path[lookahead_index],self._path[lookahead_index+1]
                travelled+=math.hypot(right[0]-left[0],right[1]-left[1]); lookahead_index+=1
            return self._path[lookahead_index]

    def _planning_details(self) -> dict:
        with self._lock:
            return {"planner":"astar_costmap" if self.config.enable_path_planning and self._planner else "direct","replans":self._replans,"avoidance_events":self._avoidance_events,"planned_path":[{"x":p[0],"y":p[1]} for p in self._path],"planned_distance_m":path_length(self._path)}

    def _run_goto(self, command_id: str, cancelled: threading.Event, requested_speed: float) -> None:
        started = time.monotonic()
        previous_heading_error: float | None = None
        previous_steering = 0.0
        previous_control_time: float | None = None
        try:
            while not cancelled.is_set():
                if not self._is_active(command_id):
                    return
                elapsed = time.monotonic() - started
                if elapsed > self.config.goal_timeout:
                    self._fail_and_stop(command_id, "goal timed out", CommandStatus.TIMEOUT)
                    return
                with self._lock:
                    startup_ready = self._startup is not None
                    target = self._target
                    pose = self._pose
                if not startup_ready:
                    if elapsed > self.config.startup_odometry_timeout:
                        self._fail_and_stop(command_id, "no odometry received before startup timeout", CommandStatus.LOCALIZATION_UNAVAILABLE)
                        return
                    cancelled.wait(self.config.control_period)
                    continue
                health = self.sensors.get_health().odometry
                if not health.fresh or health.age is None or health.age > self.config.odometry_timeout:
                    self._fail_and_stop(command_id, "odometry is stale or unavailable", CommandStatus.LOCALIZATION_UNAVAILABLE)
                    return
                with self._lock: costmap=self._costmap
                if self.config.enable_path_planning and costmap:
                    pose_cell=costmap.world_to_cell(pose.x,pose.y)
                    if costmap.is_occupied(pose_cell):
                        self._fail_and_stop(command_id,"robot departed the traversable costmap",CommandStatus.BLOCKED)
                        return
                avoidance=None
                range_health = self.sensors.get_health().range
                use_range=range_health.fresh and range_health.age is not None and range_health.age <= self.config.obstacle_timeout
                if self.config.require_obstacle_feedback and not use_range:
                    self._fail_and_stop(command_id, "front range data is stale or unavailable", CommandStatus.SENSOR_UNAVAILABLE)
                    return
                if self.config.enable_dynamic_avoidance and use_range:
                    front = self.sensors.get_front_distance()
                    if front is None or not math.isfinite(front) or front < 0:
                        self._fail_and_stop(command_id, "front range data is invalid", CommandStatus.SENSOR_UNAVAILABLE)
                        return
                    scan=self.sensors.get_range_scan()
                    avoidance=self._avoider.evaluate(scan,pose)
                    now=time.monotonic()
                    changed=self._record_dynamic_obstacles(avoidance.obstacle_points,now) if self.config.enable_dynamic_avoidance else False
                    if changed:
                        with self._lock: self._avoidance_events+=1
                    route_available=True
                    if changed and target is not None and self.config.enable_path_planning:
                        route_available=self._replan(pose,target,now)
                    if avoidance.blocked or not route_available:
                        self.motion.stop(source="autonomy")
                        with self._lock:
                            if self._blocked_since is None: self._blocked_since=now
                            blocked_for=now-self._blocked_since
                        if blocked_for >= self.config.obstacle_wait_timeout:
                            reason="dynamic obstacle did not clear before avoidance timeout" if avoidance.blocked else "A* found no route around the detected obstacle"
                            self._finish(command_id,CommandStatus.BLOCKED,reason,{"arrival_verified":False,**self._planning_details(),"front_clearance":avoidance.front_clearance})
                            return
                        cancelled.wait(self.config.control_period)
                        continue
                    with self._lock: self._blocked_since=None
                if target is None:
                    return
                distance = math.hypot(target.x - pose.x, target.y - pose.y)
                if distance <= self.config.position_tolerance:
                    details = {
                        "arrival_verified": True,
                        "verification_source": "odometry",
                        "target": target.to_dict(),
                        "odometry_pose": pose.to_dict(),
                        "distance_to_goal": distance,
                        **self._planning_details(),
                    }
                    if self.config.validation_source == "ground_truth":
                        ground_health = self.sensors.get_health().ground_truth
                        with self._lock: ground_pose = self._ground_truth_pose
                        if not ground_health.fresh or ground_pose is None:
                            self.motion.stop(source="autonomy")
                            self._finish(command_id,CommandStatus.LOCALIZATION_UNAVAILABLE,"ground-truth pose is stale or unavailable",{"arrival_verified":False,"verification_source":"ground_truth"})
                            return
                        discrepancy=math.hypot(pose.x-ground_pose.x,pose.y-ground_pose.y)
                        ground_distance=math.hypot(target.x-ground_pose.x,target.y-ground_pose.y)
                        details.update({"verification_source":"ground_truth","ground_truth_pose":ground_pose.to_dict(),"ground_truth_distance_to_goal":ground_distance,"localization_discrepancy":discrepancy})
                        if discrepancy > self.config.max_localization_discrepancy:
                            self.motion.stop(source="autonomy")
                            details["arrival_verified"]=False
                            self._finish(command_id,CommandStatus.GROUND_TRUTH_MISMATCH,"odometry and Gazebo ground truth do not verify arrival",details)
                            return
                        # Small odometry error near the threshold is expected.
                        # Keep approaching until both sources independently
                        # satisfy their configured tolerances.
                        if ground_distance <= self.config.ground_truth_tolerance:
                            self.motion.stop(source="autonomy")
                            self._finish(command_id, CommandStatus.COMPLETED, details=details)
                            return
                    else:
                        self.motion.stop(source="autonomy")
                        self._finish(command_id, CommandStatus.COMPLETED, details=details)
                        return

                tracking_x,tracking_y=self._tracking_point(pose,target)
                desired = math.atan2(tracking_y - pose.y, tracking_x - pose.x)
                heading_error = _angle(desired - pose.yaw)
                if abs(heading_error) >= self.config.reverse_required_angle and distance <= max(1.,self.config.path_lookahead*2):
                    self._fail_and_stop(command_id,"safe path recovery would require reversing without rear obstacle sensing",CommandStatus.BLOCKED)
                    return
                avoidance_bias=avoidance.steering_bias if avoidance else 0.
                if self._planner and avoidance_bias*heading_error < 0:
                    # The global path has already selected a collision-free
                    # side. Do not let noisy five-ray asymmetry counter-steer
                    # across that planned bypass.
                    avoidance_bias=0.
                now = time.monotonic()
                dt = self.config.control_period if previous_control_time is None else max(0.001, now - previous_control_time)
                heading_rate = 0.0 if previous_heading_error is None else _angle(heading_error - previous_heading_error) / dt
                requested_steering = (
                    self.config.steering_gain * heading_error
                    + self.config.steering_derivative_gain * heading_rate
                    + avoidance_bias
                )
                requested_steering = max(
                    -self.limits.max_steering_angle,
                    min(self.limits.max_steering_angle, requested_steering),
                )
                steering_step = self.config.max_steering_rate * dt
                steering = max(previous_steering-steering_step, min(previous_steering+steering_step, requested_steering))
                previous_heading_error, previous_steering, previous_control_time = heading_error, steering, now
                # An Ackermann vehicle cannot pivot. Slow it while its heading
                # is far from the goal so it turns onto the target path instead
                # of tracing a wide, high-speed circle.
                alignment = max(self.config.min_turn_speed,math.cos(heading_error))
                approach_speed=max(self.config.min_turn_speed,distance*self.config.approach_speed_gain)
                curve_factor = 1.0/(1.0+self.config.curvature_slowdown*abs(steering)/self.limits.max_steering_angle)
                speed=min(requested_speed,approach_speed)*alignment*curve_factor*(avoidance.speed_scale if avoidance else 1.)
                low_level = self.motion.drive(
                    speed,
                    steering,
                    duration=self.config.control_period * 2.5,
                    source="autonomy",
                )
                if low_level.status == CommandStatus.REJECTED:
                    reason=low_level.reason or "motion rejected"
                    status=CommandStatus.BLOCKED if "obstacle" in reason else CommandStatus.SENSOR_UNAVAILABLE if "range data" in reason else CommandStatus.FAILED
                    self._fail_and_stop(command_id,reason,status)
                    return
                if low_level.status == CommandStatus.FAILED:
                    self._fail_and_stop(command_id, low_level.reason or "motion controller failed")
                    return
                cancelled.wait(self.config.control_period)
        finally:
            with self._lock:
                self._threads.discard(threading.current_thread())

        self._finish(command_id, CommandStatus.CANCELLED, "cancelled")

    def _fail_and_stop(self, command_id: str, reason: str, status: CommandStatus=CommandStatus.FAILED) -> None:
        if self._is_active(command_id):
            self.motion.stop(source="autonomy")
        self._finish(command_id, status, reason, {"arrival_verified":False})

    def _finish(self, command_id, status, reason=None, details=None) -> None:
        with self._lock:
            result = self._by_id.get(command_id)
            if not result or result.status != CommandStatus.RUNNING:
                return
            result.status = status
            result.reason = reason
            if details: result.details.update(details)
            result.completion_time = time.time()
            self._last = result
            if self._active is result:
                self._active = None
                self._target = None

    def _is_active(self, command_id: str) -> bool:
        with self._lock:
            return bool(
                self._active
                and self._active.command_id == command_id
                and self._active.status == CommandStatus.RUNNING
            )

    def _cancel_active_locked(self, reason: str) -> CommandResult | None:
        if not self._active or self._active.status != CommandStatus.RUNNING:
            return None
        active = self._active
        self._cancel.set()
        active.status = CommandStatus.CANCELLED
        active.reason = reason
        active.completion_time = time.time()
        self._last = active
        self._active = None
        self._target = None
        return active

    def _result(self, command_type, status, reason=None):
        result = CommandResult(str(uuid.uuid4()), command_type, status, time.time(), "autonomy", reason=reason)
        self._history.append(result)
        self._by_id[result.command_id] = result
        self._last = result
        return result


class TaskCommands:
    """High-level command facade: applications supply goals, not steering.

    ``photo`` returns the latest frame supplied by the robot camera, rather
    than inventing a file or image. ``beep`` records an immediate notification
    command; the vendored RACECAR has no physical audio actuator, so callers
    can relay that completed command to their own UI or speaker bridge.
    ``report`` is supplied by :class:`RobotRuntime` and returns live SDK state.
    """

    def __init__(self, navigation: NavigationController, sensors=None, report_factory=None):
        self._navigation = navigation
        self._sensors = sensors
        self._report_factory = report_factory

    def goto(self, x, y, frame="world", speed=None):
        return self._navigation.goto(x, y, frame, speed)

    def stop(self):
        return self._navigation.stop()

    def wait(self, seconds):
        return self._navigation.wait(seconds)

    def cancel(self):
        return self._navigation.cancel()

    def status(self, command_id=None):
        return self._navigation.status(command_id)

    def photo(self):
        """Return the most recent :class:`CameraFrame` from the camera topic."""
        if self._sensors is None:
            raise RuntimeError("camera access is not configured")
        return self._sensors.get_camera_frame()

    def beep(self):
        """Record a completed notification command without affecting motion."""
        result = self._navigation._result(
            "beep", CommandStatus.RUNNING,
            "no audio actuator is fitted; emit this notification in the client or bridge",
        )
        result.start_time = result.submitted_at
        result.status = CommandStatus.COMPLETED
        result.completion_time = time.time()
        return result

    def report(self):
        """Return a JSON-serializable live state and capability report."""
        if self._report_factory is None:
            raise RuntimeError("runtime report access is not configured")
        return self._report_factory()
