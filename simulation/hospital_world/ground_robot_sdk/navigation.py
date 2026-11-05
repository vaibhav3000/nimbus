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


def _angle(value: float) -> float:
    return math.atan2(math.sin(value), math.cos(value))


@dataclass(frozen=True)
class NavigationConfig:
    position_tolerance: float = 0.10
    cruise_speed: float = 1.0
    control_period: float = 0.05
    # The RACECAR is Ackermann-steered: abrupt changes in requested steering
    # make it weave at corridor corners.  These conservative PD defaults are
    # deliberately shared by both the Gazebo adapter and the local kinematic
    # adapter, so the latter cannot hide an unstable controller.
    steering_gain: float = 0.85
    steering_derivative_gain: float = 0.12
    max_steering_rate: float = 1.50  # rad steering angle / second
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
            result = self._result("goto", CommandStatus.RUNNING)
            result.start_time = time.time()
            self._active = result
            self._target = target
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
                if self.config.require_obstacle_feedback:
                    range_health = self.sensors.get_health().range
                    if not range_health.fresh or range_health.age is None or range_health.age > self.config.obstacle_timeout:
                        self._fail_and_stop(command_id, "front range data is stale or unavailable", CommandStatus.SENSOR_UNAVAILABLE)
                        return
                    front = self.sensors.get_front_distance()
                    if front is None or not math.isfinite(front) or front < 0:
                        self._fail_and_stop(command_id, "front range data is invalid", CommandStatus.SENSOR_UNAVAILABLE)
                        return
                    safety_distance = getattr(getattr(self.motion,"safety",None),"config",None)
                    safety_distance = getattr(safety_distance,"minimum_front_distance",float(os.environ.get("NIMBUS_MINIMUM_FRONT_DISTANCE", "0.20")))
                    if front < safety_distance:
                        should_abort = True
                        if target is not None:
                            desired = math.atan2(target.y - pose.y, target.x - pose.x)
                            heading_error = _angle(desired - pose.yaw)
                            if math.cos(heading_error) <= 0.5:
                                should_abort = False
                        if should_abort:
                            self._fail_and_stop(command_id, "front obstacle is inside safety distance", CommandStatus.BLOCKED)
                            return
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

                desired = math.atan2(target.y - pose.y, target.x - pose.x)
                heading_error = _angle(desired - pose.yaw)
                now = time.monotonic()
                dt = self.config.control_period if previous_control_time is None else max(0.001, now - previous_control_time)
                # Derivative feedback damps the left/right correction cycle
                # when the vehicle crosses its desired bearing.  Wrap the
                # angular delta so a +/- pi transition cannot create a spike.
                heading_rate = 0.0 if previous_heading_error is None else _angle(heading_error - previous_heading_error) / dt
                requested_steering = (
                    self.config.steering_gain * heading_error
                    + self.config.steering_derivative_gain * heading_rate
                )
                requested_steering = max(
                    -self.limits.max_steering_angle,
                    min(self.limits.max_steering_angle, requested_steering),
                )
                steering_step = self.config.max_steering_rate * dt
                steering = max(
                    previous_steering - steering_step,
                    min(previous_steering + steering_step, requested_steering),
                )
                previous_heading_error = heading_error
                previous_steering = steering
                previous_control_time = now
                # An Ackermann vehicle cannot pivot. Slow it while its heading
                # is far from the goal and whenever it needs a tight turn.
                # This prevents a full-speed command from carrying it into a
                # corridor wall before the new heading can take effect.
                alignment = max(self.config.min_turn_speed, math.cos(heading_error))
                curve_factor = 1.0 / (
                    1.0 + self.config.curvature_slowdown * abs(steering) / self.limits.max_steering_angle
                )
                speed = min(requested_speed, max(self.config.min_turn_speed, distance)) * alignment * curve_factor
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
