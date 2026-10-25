"""Local safety policy applied before every adapter command."""
from __future__ import annotations
from dataclasses import dataclass
from .models import MotionLimits, Twist2D

@dataclass
class SafetyConfig: obstacle_guard: bool=False; minimum_front_distance: float=.25

class SafetySupervisor:
    def __init__(self, sensors, limits: MotionLimits, config: SafetyConfig | None=None): self.sensors=sensors; self.limits=limits; self.config=config or SafetyConfig(); self.emergency=False
    def sanitize_velocity(self, linear: float, angular: float) -> tuple[Twist2D | None, str | None]:
        if self.emergency and (linear or angular): return None, "emergency stop is active"
        linear=max(self.limits.min_linear_velocity, min(self.limits.max_linear_velocity, float(linear)))
        if self.config.obstacle_guard and linear > 0:
            if not self.sensors.get_health().range.fresh:
                return None, "front range data is stale or unavailable"
            front=self.sensors.get_front_distance()
            if front is not None and front < self.config.minimum_front_distance: return None, "front obstacle is inside safety distance"
        return Twist2D(linear, float(angular)), None
    def emergency_stop(self): self.emergency=True
    def clear_emergency(self): self.emergency=False
    def status(self): return {"emergency_stop":self.emergency, "obstacle_guard":self.config.obstacle_guard, "minimum_front_distance":self.config.minimum_front_distance, "sensor_health":self.sensors.get_health().to_dict()}
