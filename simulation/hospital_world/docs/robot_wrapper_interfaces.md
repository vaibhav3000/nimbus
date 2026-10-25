# Ground-Robot Wrapper Interfaces

Verified against `ground_robot_sdk` source. Start with
`RobotRuntime(...).start()` or a context manager; access high-level commands
through `robot.commands`. `CommandResult` carries command ID, status, times,
and any rejection/failure reason.

## Primary application interface — implemented

| Function | Exact Python signature | Description |
| --- | --- | --- |
| `RobotRuntime` | `RobotRuntime(adapter=None, limits: MotionLimits=MotionLimits(), safety_config: SafetyConfig \| None=None, robot_name="ground_robot", namespace="", navigation_config: NavigationConfig \| None=None)` | Creates a namespaced runtime and binds simulator sensors. |
| `runtime.start` | `start()` | Starts the configured adapter and returns the runtime. |
| `runtime.close` | `close()` | Stops navigation, motion, and the adapter safely. |
| `runtime.capabilities` | `capabilities()` | Returns declared robot limits, namespaces, topics, and features. |
| `runtime.state` | `state()` | Returns latest odometry, range, camera, health, and controller state. |
| `runtime.report` | `report()` | Returns JSON-ready state plus immutable capability metadata. |
| `commands.goto` | `goto(x, y, frame="world", speed=None)` | Starts odometry-local Ackermann point-to-point navigation. |
| `commands.stop` | `stop()` | Cancels navigation and commands an immediate motion stop. |
| `commands.cancel` | `cancel()` | Cancels the active navigation task, if any. |
| `commands.wait` | `wait(seconds)` | Completes a blocking timed task without moving. |
| `commands.status` | `status(command_id=None)` | Returns specified, active, or most recent navigation result. |
| `commands.photo` | `photo()` | Returns the latest `CameraFrame`; may be unavailable initially. |
| `commands.report` | `report()` | Returns the runtime JSON-ready state and capabilities report. |
| `commands.beep` | `beep()` | **Simulated:** completes a notification; no physical speaker exists. |

## Supplementary direct interfaces — implemented

| Function | Exact Python signature | Description |
| --- | --- | --- |
| `motion.drive` | `drive(speed, steering_angle, duration=None, source="remote") -> CommandResult` | Commands bounded Ackermann speed and steering for a duration. |
| `motion.set_velocity` | `set_velocity(linear, angular, duration=None, source="remote") -> CommandResult` | Commands linear and angular velocity for a duration. |
| `motion.forward` / `motion.backward` | `forward(speed=None, duration=None, source="remote")`<br>`backward(speed=None, duration=None, source="remote")` | Commands straight forward or reverse timed motion. |
| `motion.turn_left` / `motion.turn_right` | `turn_left(angular_speed=None, duration=None, source="remote")`<br>`turn_right(angular_speed=None, duration=None, source="remote")` | Commands moving left or right Ackermann turns. |
| `motion.execute` | `execute(command: dict, source="remote")` | Dispatches supported drive, velocity, directional, or stop dictionaries. |
| `motion.stop` | `stop(source="remote", emergency=False) -> CommandResult` | Stops motion; `emergency=True` latches the emergency stop. |
| `motion.get_command` | `get_command(command_id)` | Returns a tracked motion result by command ID. |
| `motion.metadata` | `metadata()` | Returns active command ID, source, and status. |
| `sensors.subscribe` | `subscribe(sensor: str, callback: Callable) -> Callable[[], None]` | Registers an `odometry`, `range`, or `camera` callback. |
| `sensors.get_odometry` | `get_odometry() -> Odometry` | Returns the latest odometry sample. |
| `sensors.get_range_scan` / `sensors.get_front_distance` | `get_range_scan() -> RangeScan`<br>`get_front_distance()` | Returns latest range scan or its front distance. |
| `sensors.get_camera_frame` | `get_camera_frame() -> CameraFrame` | Returns the latest camera frame cache entry. |
| `sensors.get_health` | `get_health(emergency_stop: bool=False, obstacle_guard: bool=False) -> RobotHealth` | Reports sensor availability, freshness, safety flags, and health. |
| `sensors.get_robot_state` | `get_robot_state(controller: dict \| None=None, *, emergency_stop=False, obstacle_guard=False) -> RobotState` | Returns combined cached sensors, health, and controller metadata. |
| `safety.emergency_stop` / `safety.clear_emergency` | `emergency_stop()`<br>`clear_emergency()` | Latches or clears the safety motion interlock. |
| `safety.status` | `status()` | Returns emergency, obstacle-guard, and sensor-health status. |
| `navigation.goto` | `goto(x: float, y: float, frame="world", speed: float \| None = None) -> CommandResult` | Typed direct equivalent of `commands.goto`. |
| `navigation.cancel` / `navigation.stop` | `cancel() -> CommandResult \| None`<br>`stop() -> CommandResult` | Cancels navigation or stops navigation and motion. |
| `navigation.status` / `navigation.wait` | `status(command_id=None)`<br>`wait(seconds: float) -> CommandResult` | Reads navigation result or runs a timed wait task. |

## Optional local HTTP wrapper — implemented

| Function | Exact Python signature | Description |
| --- | --- | --- |
| `LocalAPIServer` | `LocalAPIServer(runtime, host="127.0.0.1", port=8080, telemetry_hz=5.)` | Creates the optional local REST and WebSocket wrapper. |
| `api.start` / `api.shutdown` | `start()`<br>`shutdown()` | Starts or stops the local HTTP server thread. |

| HTTP interface | Method | Description |
| --- | --- | --- |
| `/health`, `/api/v1/health` | `GET` | Reports service availability. |
| `/robot/state`, `/api/v1/robot/state` | `GET` | Returns complete cached robot state. |
| `/robot/pose`, `/robot/odometry`, `/robot/range` and `/api/v1` aliases | `GET` | Returns individual pose, odometry, or range data. |
| `/api/v1/robot/capabilities`, `/robot/camera/latest` | `GET` | Returns capabilities or latest JPEG camera frame. |
| `/api/v1/commands/<command_id>` | `GET` | Returns a tracked command result. |
| `/api/v1/telemetry` | `WebSocket` | Streams pose, velocity, range, health, and controller telemetry. |
| `/robot/command`, `/api/v1/control/velocity`, `/api/v1/control/drive` | `POST` | Executes command dictionaries, velocity, or Ackermann drive requests. |
| `/robot/stop`, `/api/v1/control/stop`, `/api/v1/control/emergency-stop` | `POST` | Stops motion or latches the emergency stop. |

## Behaviour boundaries

- `goto` is implemented odometry-local navigation; it is not global planning or dynamic obstacle avoidance.
- `photo` returns real cached simulation-camera input; it can be unavailable before a message arrives.
- `beep` is a simulated completed notification only; clients must supply audio or UI notification.
- Declared unsupported capabilities: IMU (`imu=False`) and SLAM (`slam=False`).
- ROS 2/Gazebo adapter callbacks and sensor-update injection methods are intentionally excluded as internals.
