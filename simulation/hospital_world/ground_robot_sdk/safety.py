"""Hardware-local reflex safety policy.

The watchdog sits below navigation, edge, and cloud, so a stale range stream
or unsafe obstacle stops an already-published velocity command locally.
"""
from __future__ import annotations
from dataclasses import dataclass
import math
import threading
from .models import MotionLimits, Twist2D

@dataclass
class SafetyConfig:
    obstacle_guard: bool=False
    minimum_front_distance: float=.25
    lost_contact_timeout: float | None=None
    reflex_period: float=.02

class SafetySupervisor:
    def __init__(self, sensors, limits: MotionLimits, config: SafetyConfig | None=None):
        self.sensors=sensors; self.limits=limits; self.config=config or SafetyConfig(); self.emergency=False
        self.reflex_stop_reason=None; self.reflex_stop_count=0; self._stop_motion=None; self._motion_active=None
        self._closed=threading.Event(); self._thread=None; self._lock=threading.RLock()
    def attach_reflex(self, stop_motion, motion_active):
        self._stop_motion,self._motion_active=stop_motion,motion_active
        if self.config.obstacle_guard and self._thread is None:
            self._thread=threading.Thread(target=self._watch,name="ground-robot-safety-reflex",daemon=True); self._thread.start()
    def close(self):
        self._closed.set()
        if self._thread and self._thread is not threading.current_thread(): self._thread.join(timeout=max(.1,self.config.reflex_period*3))
    def _unsafe_reason(self):
        health=self.sensors.get_health().range; timeout=self.config.lost_contact_timeout
        if not health.fresh or (timeout is not None and (health.age is None or health.age > timeout)): return "front range contact lost or stale"
        front=self.sensors.get_front_distance()
        if front is None or not math.isfinite(float(front)) or float(front)<0: return "front range data is invalid"
        if float(front)<self.config.minimum_front_distance: return "front obstacle is inside safety distance"
        return None
    def _watch(self):
        while not self._closed.wait(max(.005,self.config.reflex_period)):
            if self._motion_active and self._motion_active():
                reason=self._unsafe_reason()
                if reason:
                    with self._lock: self.reflex_stop_reason=reason; self.reflex_stop_count+=1
                    try: self._stop_motion(source="safety_reflex")
                    except Exception: pass
    def sanitize_velocity(self, linear: float, angular: float) -> tuple[Twist2D | None, str | None]:
        if self.emergency and (linear or angular): return None, "emergency stop is active"
        linear=max(self.limits.min_linear_velocity, min(self.limits.max_linear_velocity, float(linear)))
        if self.config.obstacle_guard and linear>0:
            reason=self._unsafe_reason()
            if reason: return None,reason
        return Twist2D(linear,float(angular)),None
    def emergency_stop(self): self.emergency=True
    def clear_emergency(self): self.emergency=False
    def status(self): return {"emergency_stop":self.emergency,"obstacle_guard":self.config.obstacle_guard,"minimum_front_distance":self.config.minimum_front_distance,"lost_contact_timeout":self.config.lost_contact_timeout,"reflex_stop_reason":self.reflex_stop_reason,"reflex_stop_count":self.reflex_stop_count,"sensor_health":self.sensors.get_health().to_dict()}
