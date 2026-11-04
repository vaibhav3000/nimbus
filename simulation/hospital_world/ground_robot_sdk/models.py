"""Typed, JSON-serializable application models. No ROS types live here."""
from __future__ import annotations
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional


class JsonModel:
    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum): return value.value
    if isinstance(value, (bytes, bytearray)): return None
    if isinstance(value, dict): return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)): return [_jsonable(v) for v in value]
    return value


@dataclass(frozen=True)
class Pose2D(JsonModel): x: float = 0.; y: float = 0.; yaw: float = 0.
@dataclass(frozen=True)
class Twist2D(JsonModel): linear: float = 0.; angular: float = 0.
@dataclass(frozen=True)
class Odometry(JsonModel): pose: Pose2D = field(default_factory=Pose2D); velocity: Twist2D = field(default_factory=Twist2D); timestamp: Optional[float] = None
@dataclass(frozen=True)
class GroundTruthPose(JsonModel):
    """Simulator truth in the Gazebo world frame.

    This is deliberately separate from navigation odometry so physical robot
    adapters are not required to provide simulator-only feedback.
    """
    pose: Pose2D = field(default_factory=Pose2D)
    timestamp: Optional[float] = None
    frame_id: str = "world"
    child_frame_id: str = ""
@dataclass(frozen=True)
class RangeScan(JsonModel): ranges: tuple[float, ...] = (); front: Optional[float] = None; minimum: Optional[float] = None; timestamp: Optional[float] = None
@dataclass(frozen=True)
class CameraFrame(JsonModel): timestamp: Optional[float] = None; width: int = 0; height: int = 0; encoding: str = "jpeg"; available: bool = False; data: Optional[bytes] = field(default=None, repr=False, compare=False)
@dataclass(frozen=True)
class VelocityCommand(JsonModel): linear: float; angular: float; duration: Optional[float] = None
@dataclass(frozen=True)
class DriveCommand(JsonModel): speed: float; steering_angle: float; duration: Optional[float] = None
@dataclass(frozen=True)
class MotionLimits(JsonModel): wheelbase: float = .325; min_linear_velocity: float = -1.; max_linear_velocity: float = 1.; max_steering_angle: float = .55
@dataclass(frozen=True)
class SensorHealth(JsonModel): available: bool = False; fresh: bool = False; age: Optional[float] = None
@dataclass(frozen=True)
class RobotHealth(JsonModel): status: str = "unavailable"; freshness_timeout: float = 2.; odometry: SensorHealth = field(default_factory=SensorHealth); range: SensorHealth = field(default_factory=SensorHealth); camera: SensorHealth = field(default_factory=SensorHealth); emergency_stop: bool = False; obstacle_guard: bool = False; ground_truth: SensorHealth = field(default_factory=SensorHealth)
@dataclass(frozen=True)
class RobotCapabilities(JsonModel):
    robot_type: str = "mit_racecar"; motion_type: str = "ackermann"; limits: MotionLimits = field(default_factory=MotionLimits); camera: bool = True; range: bool = True; odometry: bool = True; imu: bool = False; navigation: bool = True; slam: bool = False; robot_name: str = "ground_robot"; namespace: str = ""; topics: dict[str, str] = field(default_factory=dict)
    def to_dict(self) -> dict[str, Any]:
        return {"robot_type":self.robot_type,"robot_name":self.robot_name,"namespace":self.namespace,"motion":{"type":self.motion_type,"max_linear_velocity":self.limits.max_linear_velocity,"min_linear_velocity":self.limits.min_linear_velocity,"max_steering_angle":self.limits.max_steering_angle,"wheelbase":self.limits.wheelbase},"sensors":{"camera":self.camera,"range":self.range,"odometry":self.odometry,"imu":self.imu},"topics":dict(self.topics),"navigation":self.navigation,"slam":self.slam}

class CommandStatus(str, Enum):
    ACCEPTED="accepted"; PENDING="pending"; RUNNING="running"; COMPLETED="completed"
    CANCELLED="cancelled"; FAILED="failed"; REJECTED="rejected"; BLOCKED="blocked"
    INVALID_DESTINATION="invalid_destination"; SENSOR_UNAVAILABLE="sensor_unavailable"
    LOCALIZATION_UNAVAILABLE="localization_unavailable"; GROUND_TRUTH_MISMATCH="ground_truth_mismatch"
    TIMEOUT="timeout"; CAPTURE_FAILED="capture_failed"
@dataclass
class CommandResult(JsonModel):
    command_id: str; command_type: str; status: CommandStatus; submitted_at: float; source: str; start_time: Optional[float] = None; completion_time: Optional[float] = None; reason: Optional[str] = None; details: dict[str, Any] = field(default_factory=dict)
@dataclass(frozen=True)
class RobotState(JsonModel): odometry: Odometry; range: RangeScan; camera: CameraFrame; health: RobotHealth; controller: dict[str, Any] = field(default_factory=dict); ground_truth: Optional[GroundTruthPose] = None
