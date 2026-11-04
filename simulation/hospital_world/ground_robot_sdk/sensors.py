"""Typed latest-value sensor cache and application callbacks."""
from __future__ import annotations
import threading, time
from typing import Callable
from .models import CameraFrame, GroundTruthPose, Odometry, RangeScan, RobotHealth, RobotState, SensorHealth


class SensorManager:
    def __init__(self, freshness_timeout: float = 2.0):
        self._timeout, self._lock = freshness_timeout, threading.RLock()
        self._odom, self._range, self._camera, self._ground_truth = Odometry(), RangeScan(), CameraFrame(), None
        self._received = {"odometry": None, "range": None, "camera": None, "ground_truth": None}
        self._callbacks: dict[str, list[Callable]] = {"odometry": [], "range": [], "camera": [], "ground_truth": []}
    def subscribe(self, sensor: str, callback: Callable) -> Callable[[], None]:
        if sensor not in self._callbacks: raise ValueError(f"Unknown sensor: {sensor}")
        with self._lock: self._callbacks[sensor].append(callback)
        return lambda: self._callbacks[sensor].remove(callback)
    def update_odometry(self, value: Odometry) -> None: self._update("odometry", value)
    def update_range_scan(self, value: RangeScan) -> None: self._update("range", value)
    def update_camera(self, value: CameraFrame) -> None: self._update("camera", value)
    def update_ground_truth(self, value: GroundTruthPose) -> None: self._update("ground_truth", value)
    def _update(self, name: str, value) -> None:
        with self._lock:
            attribute = {"odometry": "_odom", "range": "_range", "camera": "_camera", "ground_truth": "_ground_truth"}[name]
            setattr(self, attribute, value); self._received[name] = time.monotonic(); callbacks = list(self._callbacks[name])
        for callback in callbacks:
            try: callback(value)
            except Exception: pass
    def get_odometry(self) -> Odometry:
        with self._lock: return self._odom
    def get_range_scan(self) -> RangeScan:
        with self._lock: return self._range
    def get_front_distance(self): return self.get_range_scan().front
    def get_camera_frame(self) -> CameraFrame:
        with self._lock: return self._camera
    def get_ground_truth(self) -> GroundTruthPose | None:
        with self._lock: return self._ground_truth
    def get_health(self, emergency_stop: bool=False, obstacle_guard: bool=False) -> RobotHealth:
        with self._lock:
            now=time.monotonic(); values={name: SensorHealth(at is not None, at is not None and now-at <= self._timeout, None if at is None else max(0., now-at)) for name, at in self._received.items()}
        required = (values["odometry"], values["range"])
        status="healthy" if all(x.fresh for x in required) else "degraded" if any(x.available for x in required) else "unavailable"
        return RobotHealth(status, self._timeout, values["odometry"], values["range"], values["camera"], emergency_stop, obstacle_guard, values["ground_truth"])
    def get_robot_state(self, controller: dict | None=None, *, emergency_stop=False, obstacle_guard=False) -> RobotState:
        return RobotState(self.get_odometry(), self.get_range_scan(), self.get_camera_frame(), self.get_health(emergency_stop, obstacle_guard), controller or {}, self.get_ground_truth())
