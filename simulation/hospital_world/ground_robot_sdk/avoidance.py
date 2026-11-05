"""Reactive dynamic-obstacle interpretation for narrow front range scans."""
from __future__ import annotations

from dataclasses import dataclass
import math

from .models import Pose2D, RangeScan


@dataclass(frozen=True)
class AvoidanceConfig:
    safety_distance: float = 0.25
    caution_distance: float = 1.50
    obstacle_inflation: float = 0.85
    maximum_steering_bias: float = 0.30
    minimum_speed_scale: float = 0.20


@dataclass(frozen=True)
class AvoidanceDecision:
    blocked: bool
    speed_scale: float
    steering_bias: float
    obstacle_points: tuple[tuple[float,float], ...]
    front_clearance: float | None


class DynamicObstacleAvoider:
    """Convert a validated scan into slowdown, steering, and obstacle points."""

    def __init__(self, config: AvoidanceConfig = AvoidanceConfig()): self.config=config

    def evaluate(self, scan: RangeScan, pose: Pose2D) -> AvoidanceDecision:
        samples=[]; points=[]
        count=len(scan.ranges)
        increment=scan.angle_increment if scan.angle_increment is not None else (0.2/max(1,count-1))
        angle_min=scan.angle_min if scan.angle_min is not None else (-increment*(count-1)/2)
        for index,value in enumerate(scan.ranges):
            value=float(value)
            if not math.isfinite(value) or value <= 0: continue
            angle=angle_min+index*increment
            samples.append((angle,value))
            if value <= self.config.caution_distance:
                bearing=pose.yaw+angle
                points.append((pose.x+value*math.cos(bearing),pose.y+value*math.sin(bearing)))
        front=scan.front
        if front is None and samples:
            central=[value for angle,value in samples if abs(angle) <= max(abs(increment),.12)]
            front=min(central or [value for _,value in samples])
        if front is None or not math.isfinite(float(front)):
            return AvoidanceDecision(False,1.,0.,tuple(points),None)
        front=float(front)
        blocked=front < self.config.safety_distance
        if front >= self.config.caution_distance:
            return AvoidanceDecision(False,1.,0.,tuple(points),front)
        left=[value for angle,value in samples if angle > 0]; right=[value for angle,value in samples if angle < 0]
        left_clear=max(left,default=front); right_clear=max(right,default=front)
        difference=(left_clear-right_clear)/max(self.config.caution_distance,1e-6)
        bias=max(-self.config.maximum_steering_bias,min(self.config.maximum_steering_bias,difference*self.config.maximum_steering_bias))
        scale=max(self.config.minimum_speed_scale,min(1.,(front-self.config.safety_distance)/max(self.config.caution_distance-self.config.safety_distance,1e-6)))
        return AvoidanceDecision(blocked,scale,bias,tuple(points),front)
