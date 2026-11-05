"""Main public SDK entry point."""
from __future__ import annotations
from .arbiter import CommandArbiter
from .controller import RobotController
from .gazebo_adapter import GazeboAdapter
from .models import MotionLimits, RobotCapabilities
from .namespaces import RobotIdentity
from .navigation import NavigationConfig, NavigationController, TaskCommands
from .safety import SafetyConfig, SafetySupervisor
from .sensors import SensorManager

class RobotRuntime:
    def __init__(self, adapter=None, limits: MotionLimits=MotionLimits(), safety_config: SafetyConfig | None=None, robot_name="ground_robot", namespace="", navigation_config: NavigationConfig | None=None):
        self.identity=RobotIdentity(robot_name,namespace); self.topics=self.identity.topics
        self.limits=limits; self.adapter=adapter or GazeboAdapter(identity=self.identity); self.sensors=SensorManager(); self.adapter.bind_sensor_callbacks(self.sensors.update_odometry,self.sensors.update_range_scan,self.sensors.update_camera,self.sensors.update_ground_truth)
        self.arbiter=CommandArbiter(); self.safety=SafetySupervisor(self.sensors,limits,safety_config); self.controller=RobotController(self.adapter,self.sensors,limits,self.arbiter,self.safety); self.safety.attach_reflex(self.controller.stop,self.controller.is_motion_active); self.motion=self.controller
        self.navigation=NavigationController(self.motion,self.sensors,limits,navigation_config)
        self.commands=TaskCommands(self.navigation, self.sensors, self.report)
    def start(self): self.adapter.start(); return self
    def close(self):
        # Navigation joins its worker before the controller and adapter are
        # torn down, so no background task can publish into a closed ROS context.
        self.safety.close()
        self.navigation.close()
        self.controller.close()
        self.adapter.close()
    def capabilities(self):
        return RobotCapabilities(limits=self.limits, robot_name=self.identity.robot_name, namespace=self.identity.namespace, topics=self.topics.__dict__)
    def state(self): return self.sensors.get_robot_state(self.controller.metadata(),emergency_stop=self.safety.emergency,obstacle_guard=self.safety.config.obstacle_guard)
    def report(self):
        """Return live robot state and immutable capability metadata."""
        return {"state": self.state().to_dict(), "capabilities": self.capabilities().to_dict()}
    def readiness(self, require_ground_truth=False):
        health=self.state().health
        middleware_ready=bool(getattr(self.adapter,"middleware_ready",getattr(self.adapter,"started",False)))
        required=[health.odometry.fresh,health.range.fresh]
        if require_ground_truth: required.append(health.ground_truth.fresh)
        if middleware_ready and all(required) and self.navigation is not None: return "READY"
        if middleware_ready and any((health.odometry.available,health.range.available,health.ground_truth.available)): return "DEGRADED"
        return "DISCONNECTED" if not middleware_ready else "INITIALIZING"
    def __enter__(self): return self.start()
    def __exit__(self,*_): self.close()
