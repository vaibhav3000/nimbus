"""Motion API, bounded command tracking, arbitration, and safety."""
from __future__ import annotations

import math
import threading
import time
import uuid
from collections import deque

from .arbiter import CommandArbiter
from .models import CommandResult, CommandStatus, MotionLimits, Twist2D


class RobotController:
    """Serialises all velocity publication and owns timed-command shutdown."""

    def __init__(self, adapter, sensors, limits: MotionLimits = MotionLimits(), arbiter=None, safety=None, command_duration=0.1, history_size=128):
        self.adapter = adapter
        self.sensors = sensors
        self.limits = limits
        self.arbiter = arbiter or CommandArbiter()
        self.safety = safety
        self.default_duration = command_duration
        self._history = deque(maxlen=history_size)
        self._by_id = {}
        self._timer = None
        self._lock = threading.RLock()
        self._active_id = None
        self._closed = False

    def drive(self, speed, steering_angle, duration=None, source="remote") -> CommandResult:
        speed = max(self.limits.min_linear_velocity, min(self.limits.max_linear_velocity, float(speed)))
        delta = max(-self.limits.max_steering_angle, min(self.limits.max_steering_angle, float(steering_angle)))
        omega = 0.0 if abs(speed) < 1e-6 else speed / self.limits.wheelbase * math.tan(delta)
        return self._submit("drive", Twist2D(speed, omega), duration, source)

    def set_velocity(self, linear, angular, duration=None, source="remote") -> CommandResult:
        return self._submit("velocity", Twist2D(float(linear), float(angular)), duration, source)

    def forward(self, speed=None, duration=None, source="remote"):
        return self.drive(abs(speed if speed is not None else 0.2), 0.0, duration, source)

    def backward(self, speed=None, duration=None, source="remote"):
        return self.drive(-abs(speed if speed is not None else 0.2), 0.0, duration, source)

    def turn_left(self, angular_speed=None, duration=None, source="remote"):
        return self.set_velocity(0.2, abs(angular_speed if angular_speed is not None else 0.7), duration, source)

    def turn_right(self, angular_speed=None, duration=None, source="remote"):
        return self.set_velocity(0.2, -abs(angular_speed if angular_speed is not None else 0.7), duration, source)

    def execute(self, command: dict, source="remote"):
        if "speed" in command and "steering_angle" in command:
            return self.drive(command["speed"], command["steering_angle"], command.get("duration"), source)
        if "linear" in command or "angular" in command:
            return self.set_velocity(command.get("linear", 0), command.get("angular", 0), command.get("duration"), source)
        action = command.get("action")
        if action == "stop":
            return self.stop(source=source)
        if action in {"forward", "backward"}:
            return getattr(self, action)(command.get("speed"), command.get("duration"), source)
        if action in {"turn_left", "turn_right"}:
            return getattr(self, action)(command.get("angular_speed", command.get("speed")), command.get("duration"), source)
        return self._result("unknown", CommandStatus.REJECTED, source, "unsupported command")

    def stop(self, source="remote", emergency=False) -> CommandResult:
        if emergency and self.safety:
            self.safety.emergency_stop()
            source = "emergency"
        with self._lock:
            self._cancel_timer_locked()
            active = self.get_command(self._active_id) if self._active_id else None
            if active and active.status == CommandStatus.RUNNING:
                active.status = CommandStatus.CANCELLED
                active.completion_time = time.time()
                self.arbiter.release(active.source)
            self._active_id = None
            error = self._stop_adapter_locked()
            self.arbiter.release(source)
            return self._result("stop", CommandStatus.FAILED if error else CommandStatus.COMPLETED, source, error)

    def close(self) -> None:
        """Cancel timers and issue one best-effort stop before adapter teardown."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._cancel_timer_locked()
            active = self.get_command(self._active_id) if self._active_id else None
            if active and active.status == CommandStatus.RUNNING:
                active.status = CommandStatus.CANCELLED
                active.reason = "controller closed"
                active.completion_time = time.time()
                self.arbiter.release(active.source)
            self._active_id = None
            self._stop_adapter_locked()
            self.arbiter.release("autonomy")

    def get_command(self, command_id):
        return self._by_id.get(command_id)

    def metadata(self):
        active = self.get_command(self._active_id) if self._active_id else None
        return {
            "active_command_id": self._active_id,
            "active_source": self.arbiter.active_source,
            "command_status": None if not active else active.status.value,
        }

    def is_motion_active(self) -> bool:
        """True while the controller has an active velocity command."""
        with self._lock:
            active = self.get_command(self._active_id) if self._active_id else None
            return bool(active and active.status == CommandStatus.RUNNING)

    def _submit(self, kind, requested: Twist2D, duration, source):
        duration = self.default_duration if duration is None else float(duration)
        if duration < 0:
            return self._result(kind, CommandStatus.REJECTED, source, "duration must be non-negative")
        with self._lock:
            if self._closed:
                return self._result(kind, CommandStatus.REJECTED, source, "controller is closed")
            allowed, reason = self.arbiter.authorize(source, duration)
            if not allowed:
                return self._result(kind, CommandStatus.REJECTED, source, reason)
            command, reason = self.safety.sanitize_velocity(requested.linear, requested.angular) if self.safety else (requested, None)
            if command is None:
                self.arbiter.release(source)
                return self._result(kind, CommandStatus.REJECTED, source, reason)
            self._cancel_timer_locked()
            previous = self.get_command(self._active_id) if self._active_id else None
            if previous and previous.status == CommandStatus.RUNNING:
                previous.status = CommandStatus.CANCELLED
                previous.completion_time = time.time()
            result = self._result(kind, CommandStatus.RUNNING, source)
            result.start_time = time.time()
            try:
                self.adapter.set_velocity(command)
            except Exception as exc:
                result.status = CommandStatus.FAILED
                result.reason = f"adapter failed to apply velocity: {exc}"
                result.completion_time = time.time()
                self.arbiter.release(source)
                return result
            self._active_id = result.command_id
            self._timer = threading.Timer(duration, self._complete, (result.command_id, source))
            self._timer.daemon = True
            self._timer.start()
            return result

    def _complete(self, command_id, source):
        with self._lock:
            result = self.get_command(command_id)
            if self._closed or not result or self._active_id != command_id:
                return
            error = self._stop_adapter_locked()
            result.status = CommandStatus.FAILED if error else CommandStatus.COMPLETED
            result.reason = error
            result.completion_time = time.time()
            self._active_id = None
            self._timer = None
            self.arbiter.release(source)

    def _cancel_timer_locked(self) -> None:
        if self._timer:
            self._timer.cancel()
            self._timer = None

    def _stop_adapter_locked(self) -> str | None:
        try:
            self.adapter.stop()
        except Exception as exc:
            # Shutdown must not leave a timer or navigation worker throwing in
            # the background after middleware has invalidated its context.
            return f"adapter failed to stop: {exc}"
        return None

    def _result(self, kind, status, source, reason=None):
        value = CommandResult(str(uuid.uuid4()), kind, status, time.time(), source, reason=reason)
        self._history.append(value)
        self._by_id[value.command_id] = value
        return value
