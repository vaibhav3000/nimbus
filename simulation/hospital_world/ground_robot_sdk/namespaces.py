"""Robot identity, namespaced topic layout, and collision-free frame IDs."""
from __future__ import annotations
from dataclasses import dataclass

@dataclass(frozen=True)
class RobotTopics:
    cmd_vel: str; odom: str; range_scan: str; camera_image: str; tf: str; pose: str
    odom_frame: str; base_frame: str

@dataclass(frozen=True)
class RobotIdentity:
    """Empty namespace preserves the original single-robot topic contract."""
    robot_name: str = "ground_robot"
    namespace: str = ""
    def __post_init__(self):
        if not self.robot_name or "/" in self.robot_name: raise ValueError("robot_name must be a non-empty name without '/'")
        normalized=self.namespace.strip()
        if normalized and not normalized.startswith("/"): normalized="/"+normalized
        object.__setattr__(self,"namespace",normalized.rstrip("/"))
    @property
    def topics(self) -> RobotTopics:
        if not self.namespace:
            return RobotTopics("/cmd_vel","/odom","/picarx/ultrasonic/scan","/picarx/camera/image","/tf","/picarx/pose","odom","base_link")
        prefix=self.namespace
        return RobotTopics(f"{prefix}/cmd_vel",f"{prefix}/odom",f"{prefix}/ultrasonic/scan",f"{prefix}/camera/image",f"{prefix}/tf",f"{prefix}/pose",f"{self.robot_name}/odom",f"{self.robot_name}/base_link")
