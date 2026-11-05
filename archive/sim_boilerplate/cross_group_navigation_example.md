# Cross-Group Navigation: Ward 2B → Ward 2C

**Scenario.** Ward 2B resolves to `east_waiting_approach` (`[4.5, 8.0]` m,
east). Ward 2C resolves to `west_waiting_approach` (`[-4.5, 8.0]` m, west).
Those are logical PRD aliases, not asserted room or bed coordinates.

**Verified Dijkstra route (19.84 m).**
`east_waiting_approach → east_mid_hall → nurse_station_approach → west_mid_hall → west_waiting_approach`

All listed edges occur in `hospital_topology.json` and are bidirectional.

| Step | Edge / group transition | Destination world `(x, y)` m | Cost (m) | Cap (m/s) | SDK command |
| ---: | --- | ---: | ---: | ---: | --- |
| 1 | `east_waiting_approach` (east) → `east_mid_hall` (east) | `(4.5, 3.0)` | 5.00 | 0.30 | `commands.goto(*sdk_xy, frame="world", speed=0.30)` |
| 2 | `east_mid_hall` (east) → `nurse_station_approach` (shared) | `(0.0, 5.0)` | 4.92 | 0.25 | `commands.goto(*sdk_xy, frame="world", speed=0.25)` |
| 3 | `nurse_station_approach` (shared) → `west_mid_hall` (west) | `(-4.5, 3.0)` | 4.92 | 0.25 | `commands.goto(*sdk_xy, frame="world", speed=0.25)` |
| 4 | `west_mid_hall` (west) → `west_waiting_approach` (west) | `(-4.5, 8.0)` | 5.00 | 0.30 | `commands.goto(*sdk_xy, frame="world", speed=0.30)` |

The cap is `min(source.speed_cap, destination.speed_cap)`. The SDK does **not**
read topology caps; the application supplies `speed` explicitly.

## Execution flow

`"Ward 2B"` → `resolve_destination()` → physical start node →
`shortest_route()` → graph waypoints → `world_to_sdk_startup()` →
`commands.goto()` → poll `commands.status()` → Ward 2C.

For this scenario, robot_1 is assumed to start at Ward 2B's physical waypoint
with world pose `(4.5, 8.0, -π/2)`. That is a scenario precondition; the
normal deployment's robot_1 spawn is `north_hall`, not Ward 2B.

```python
import time

from ground_robot_sdk import RobotRuntime, SafetyConfig
from ground_robot_sdk.models import CommandStatus
from topology.resolver import load_data, resolve_destination, shortest_route, world_to_sdk_startup

START_WORLD_POSE = (4.5, 8.0, -1.5707963267948966)  # Ward 2B; south-facing
route = shortest_route("east_waiting_approach", "Ward 2C")
assert route["nodes"] == [
    "east_waiting_approach", "east_mid_hall", "nurse_station_approach",
    "west_mid_hall", "west_waiting_approach",
]
source = resolve_destination("Ward 2B")
destination = resolve_destination("Ward 2C")
topology, waypoint_document, _ = load_data()

with RobotRuntime(
    robot_name="robot_1", namespace="/robot_1",
    safety_config=SafetyConfig(obstacle_guard=True, minimum_front_distance=0.25),
) as robot:
    for source_node, target_node in zip(route["nodes"], route["nodes"][1:]):
        speed = min(topology["nodes"][source_node]["speed_cap"],
                    topology["nodes"][target_node]["speed_cap"])
        sdk_xy = world_to_sdk_startup(
            waypoint_document["waypoints"][target_node], START_WORLD_POSE,
        )
        task = robot.commands.goto(*sdk_xy, frame="world", speed=speed)
        while True:  # goto() returns CommandResult immediately; it is asynchronous.
            result = robot.commands.status(task.command_id)
            if result.status == CommandStatus.COMPLETED:
                break
            if result.status in {CommandStatus.FAILED, CommandStatus.CANCELLED, CommandStatus.REJECTED}:
                robot.commands.stop()
                raise RuntimeError(result.reason)
            time.sleep(0.10)
    robot.commands.stop()
```

`goto(..., frame="world")` means the SDK's **startup-local odometry frame**,
not Gazebo global world. For world target `(wx, wy)` and startup pose
`(sx, sy, yaw)`, the resolver applies:

```text
dx = wx - sx; dy = wy - sy
x_sdk = cos(yaw) * dx + sin(yaw) * dy
y_sdk = -sin(yaw) * dx + cos(yaw) * dy
```

For the shown south-facing start, the four SDK goals are approximately
`(5, 0)`, `(3, -4.5)`, `(5, -9)`, and `(0, -9)`.

## Operational boundaries

- This route has **no exclusive-gate nodes**. If a route uses `lift_lobby`,
  `east_north_gate`, or `west_north_gate`, an external scheduler must reserve
  its `reservation_id`; no SDK gate-reservation client or Gazebo door actuation exists.
- `SafetyConfig(obstacle_guard=True)` rejects forward commands on stale range
  data or an obstacle closer than 0.25 m. It does not steer around obstacles.
- The Ackermann `goto` controller is odometry point-to-point control, not a
  global planner, recovery behavior, dynamic obstacle avoidance, or replanner.
- Topology weights are centreline metres; physical clearance was validated for
  the documented RACECAR envelope, not for arbitrary robot footprints.

## Reproducible MP4 recording

`scripts/record_cross_group_navigation.sh` creates a temporary scenario world,
runs the route from a real headless Fortress camera stream at 4× simulation
real-time (the per-edge SDK speed requests remain the table values).

For an existing `custom_hospital_world` GUI session, the recording used for
this example is produced by the following script. It resets only `robot_1` to
the Ward 2B waypoint through `/world/hospital/set_pose`, records its real
camera stream, and uses `--speed 0.8` to keep capture short. That capture speed
is intentionally not a topology speed-cap enforcement demonstration.

```bash
./scripts/record_live_cross_group_navigation.sh --speed 0.8
# artifacts/cross_group_navigation.mp4
```
