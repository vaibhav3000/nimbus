# Hospital topology and PRD locations

This directory separates the physically verified first-floor Gazebo topology
from the PRD's logical hospital vocabulary. `hospital_topology.json` describes
only wall/asset-verified circulation waypoints. `scenario_locations.json` maps
PRD labels such as Ward 2B onto those physical waypoints; it does not claim
that the 3D scene contains clinically identified wards, bed counts, or a
coordinate for an individual bed.

## Files and schema

- `hospital_topology.json` — `topo-v0`: `version`, `note`, `nodes`, `edges`,
  and `groups`. Every node has `kind`, physical `area`, `wing`, and `speed_cap`.
- `topology_waypoints.json` — node-to-`[x, y]` mapping in the Gazebo `world`
  frame, metres.
- `scenario_locations.json` — stable logical PRD IDs and aliases resolved to
  physical node IDs.
- `resolver.py` — importable alias lookup, Dijkstra routing, and world-to-SDK
  startup-frame conversion. It does not launch Gazebo.
- `hospital_topology.png` — wall collision mesh with coloured group overlay.

`groups` replaces the earlier `clusters` property. A node belongs to exactly
one group and has the matching `wing` value. Edges appear once but are
bidirectional.

## Physical nodes

| Node | Kind | Wing | Area | Speed cap (m/s) |
| --- | --- | --- | --- | ---: |
| `lift_lobby` | gate | shared | north elevator portal lobby | 0.35 |
| `north_hall` | corridor | shared | north central corridor | 0.45 |
| `west_north_gate` | gate | west | west north room-band doorway | 0.30 |
| `east_north_gate` | gate | east | east north room-band doorway | 0.30 |
| `west_waiting_approach` | zone | west | west waiting seating approach | 0.30 |
| `east_waiting_approach` | zone | east | east waiting seating approach | 0.30 |
| `west_mid_hall` | corridor | west | west central bypass | 0.40 |
| `east_mid_hall` | corridor | east | east central bypass | 0.40 |
| `nurse_station_approach` | zone | shared | nurse-station approach | 0.25 |
| `west_south_hall` | corridor | west | west south corridor | 0.40 |
| `east_south_hall` | corridor | east | east south corridor | 0.40 |
| `south_equipment_approach` | zone | shared | south equipment-room approach | 0.25 |
| `south_room_approach` | zone | shared | south central room approach | 0.30 |

Groups are:

- `east`: `east_north_gate`, `east_waiting_approach`, `east_mid_hall`,
  `east_south_hall`.
- `west`: `west_north_gate`, `west_waiting_approach`, `west_mid_hall`,
  `west_south_hall`.
- `shared`: `lift_lobby`, `north_hall`, `nurse_station_approach`,
  `south_equipment_approach`, `south_room_approach`.

## Gate scheduling policy (SOP-GATE-03)

`lift_lobby`, `west_north_gate`, and `east_north_gate` have `exclusive: true`.
This is a scheduler-level single-lane reservation policy, not a claim that
Gazebo actuates a door. A scheduler must reserve the independent IDs
`gate:lift_lobby`, `gate:west_north_gate`, and `gate:east_north_gate`; a robot
must not stop while holding one of these passage reservations.

## Edges and weights

All routes are bidirectional. Weight is direct centreline distance in metres,
not a time prediction or a full vehicle trajectory.

| Source | Destination | Weight (m) |
| --- | --- | ---: |
| lift_lobby | north_hall | 6.00 |
| north_hall | west_north_gate | 4.92 |
| north_hall | east_north_gate | 4.92 |
| north_hall | nurse_station_approach | 7.00 |
| west_north_gate | west_waiting_approach | 6.00 |
| east_north_gate | east_waiting_approach | 6.00 |
| west_waiting_approach | west_mid_hall | 5.00 |
| east_waiting_approach | east_mid_hall | 5.00 |
| west_mid_hall | nurse_station_approach | 4.92 |
| east_mid_hall | nurse_station_approach | 4.92 |
| west_mid_hall | west_south_hall | 11.00 |
| east_mid_hall | east_south_hall | 11.00 |
| west_south_hall | south_equipment_approach | 8.32 |
| east_south_hall | south_equipment_approach | 8.32 |
| south_equipment_approach | south_room_approach | 13.00 |

## Waypoints and coordinate frame

Coordinates are `[x, y]` in Gazebo `world`, metres:

| Node | x | y |
| --- | ---: | ---: |
| lift_lobby | 0.0 | 18.0 |
| north_hall | 0.0 | 12.0 |
| west_north_gate | -4.5 | 14.0 |
| east_north_gate | 4.5 | 14.0 |
| west_waiting_approach | -4.5 | 8.0 |
| east_waiting_approach | 4.5 | 8.0 |
| west_mid_hall | -4.5 | 3.0 |
| east_mid_hall | 4.5 | 3.0 |
| nurse_station_approach | 0.0 | 5.0 |
| west_south_hall | -4.5 | -8.0 |
| east_south_hall | 4.5 | -8.0 |
| south_equipment_approach | 0.0 | -15.0 |
| south_room_approach | 0.0 | -28.0 |

## PRD scenario mappings

| PRD logical destination | Physical node | Metadata |
| --- | --- | --- |
| Ward 2B | `east_waiting_approach` | paediatric; SC-1 |
| Bed 2B-12 | `east_waiting_approach` | SC-1 delivery destination |
| Ward 2C | `west_waiting_approach` | isolation; SC-3 |
| Ward 3 | `south_room_approach` | T1 |
| Equipment store | `south_equipment_approach` | shared pickup |
| Nurse station | `nurse_station_approach` | shared zone |

The logical locations are intentionally aliases. In particular, neither Ward
2B nor Bed 2B-12 establishes a physical bed coordinate or capacity in the 3D
world.

## Robot deployment and SDK coordinates

`config/robot_deployment.json` retains independent topics and controllers.

| Robot | Namespace | Spawn `[x, y, yaw]` | Physical association | Wing |
| --- | --- | --- | --- | --- |
| robot_1 | `/robot_1` | `[0.0, 12.0, 0.0]` | exact `north_hall` waypoint | shared |
| robot_2 | `/robot_2` | `[0.0, 9.5, 0.0]` | on `north_hall` ↔ `nurse_station_approach` edge | shared |

`robot_2` deliberately has no `start_node`: its spawn is 2.5 m south of
`north_hall` on that verified edge. This avoids falsely assigning it to an east
or west node. Both spawn envelopes are checked by `validate_topology.py`.

The SDK's `goto(..., frame="world")` uses the startup-local odometry frame,
not Gazebo's global `world`. For a global world target `(wx, wy)` and configured
startup pose `(sx, sy, yaw)`, pass this SDK target:

```text
dx = wx - sx; dy = wy - sy
x_sdk = cos(yaw) * dx + sin(yaw) * dy
y_sdk = -sin(yaw) * dx + cos(yaw) * dy
```

Use the resolver rather than duplicating that calculation:

```python
from topology.resolver import resolve_for_robot, shortest_route
from ground_robot_sdk import RobotRuntime

goal = resolve_for_robot("2B-12", "robot_1")
route = shortest_route("north_hall", "2B-12")
print(route["nodes"], route["cost_m"])

with RobotRuntime(robot_name="robot_1", namespace="/robot_1") as robot:
    robot.commands.goto(*goal["sdk_startup_waypoint"], frame="world", speed=0.30)
```

`shortest_route` is Dijkstra over the documented graph weights. It is not a
collision-aware trajectory planner; follow the graph edges and retain the
RACECAR's turning/clearance limits.

## Regenerate and validate

```bash
cd /root/nimbus/simulation/hospital_world
python3 scripts/generate_topology.py
python3 scripts/validate_topology.py
python3 -m unittest discover -s tests -v
```

The validator checks the `groups` partition and wings, PRD mappings, gate
reservation metadata, deployment/start association, waypoint and edge weights,
graph connectivity, and Fortress contact clearance. Physics validation uses
13 junction envelopes, 15 swept edge envelopes, and 2 RACECAR spawn envelopes.

## Known limitations

- PRD wards/beds are logical aliases, not surveyed clinical labels or bed
  coordinates in the Gazebo scene.
- Edge weights are straight-line metres, not time, steering, or congestion
  costs.
- The clearance test uses a conservative 0.65 x 0.35 m RACECAR envelope and a
  0.65 m junction square; another chassis or steering controller needs its own
  validation.
- The SDK point-to-point controller has no global obstacle-aware planner or
  automatic gate reservation client.
