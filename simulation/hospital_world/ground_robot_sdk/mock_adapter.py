"""Fast in-memory adapter for SDK and API tests."""
from __future__ import annotations
from .adapter import RobotAdapter
from .models import CameraFrame, GroundTruthPose, Odometry, RangeScan, Twist2D


class MockAdapter(RobotAdapter):
    def __init__(self): self.started=False; self.last_command=Twist2D(); self.commands=[]
    def start(self): self.started=True
    def close(self): self.started=False
    def set_velocity(self, command: Twist2D): self.last_command=command; self.commands.append(command)
    def set_odometry(self, value: Odometry): self._odometry_callback(value)
    def set_range_scan(self, value: RangeScan): self._range_callback(value)
    def set_camera_frame(self, value: CameraFrame): self._camera_callback(value)
    def set_ground_truth(self, value: GroundTruthPose): self._ground_truth_callback(value)
