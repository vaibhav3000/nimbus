"""Hardware / simulator boundary for the SDK."""
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Callable, Optional
from .models import CameraFrame, GroundTruthPose, Odometry, RangeScan, Twist2D


class RobotAdapter(ABC):
    """Only adapters may know middleware or hardware message types."""
    def bind_sensor_callbacks(self, odometry: Callable[[Odometry], None], range_scan: Callable[[RangeScan], None], camera: Callable[[CameraFrame], None], ground_truth: Optional[Callable[[GroundTruthPose], None]] = None) -> None:
        self._odometry_callback, self._range_callback, self._camera_callback = odometry, range_scan, camera
        self._ground_truth_callback = ground_truth or (lambda _value: None)
    @abstractmethod
    def start(self) -> None: ...
    @abstractmethod
    def close(self) -> None: ...
    @abstractmethod
    def set_velocity(self, command: Twist2D) -> None: ...
    def drive(self, command: Twist2D) -> None: self.set_velocity(command)
    def stop(self) -> None: self.set_velocity(Twist2D())
