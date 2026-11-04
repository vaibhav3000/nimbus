# Nimbus Hospital Simulation

An offline-capable Gazebo Fortress hospital simulation for MIT-RACECAR-like
Ackermann robots. It packages the first-floor hospital from
[`ZimaUSP/d_hospital_world`](https://github.com/ZimaUSP/d_hospital_world), its
locally vendored Fuel assets, a Fortress-native RACECAR model, a namespaced
Python SDK, and a collision-checked `topo-v0` navigation graph.

The project is standalone: normal launches do not fetch assets or depend on a
separate robot checkout.

## Features and architecture

- Gazebo Fortress / Ignition Gazebo 6 hospital world with DART physics,
  collisions, lighting, and local meshes.
- One or two independently namespaced MIT-RACECAR-like robots using the
  Fortress Ackermann plugin.
- RGB camera, five-ray front ultrasonic/lidar, odometry, TF, pose, and velocity
  interfaces per robot.
- Python SDK with safety arbitration, Ackermann `goto`, sensor state, HTTP API,
  and isolated ROS 2 namespaces.
- First-floor topology, world-frame waypoints, a labelled floor-plan overlay,
  and runtime physical-clearance checks.

```text
Gazebo Fortress <-> /robot_N/{cmd_vel, odom, camera/image, ultrasonic/scan}
       ^                         ^
 hospital.sdf              ground_robot_sdk (optional ROS 2 bridge)
       ^                         ^
 topology/              RobotRuntime(robot_name, namespace)
```

## Requirements

- Gazebo Fortress: `ign gazebo` major version 6 (`ignition-gazebo6`). This
  package was validated with 6.16.0.
- Python 3.10+ for utilities and SDK tests. The basic SDK tests use only the
  standard library; `requirements.txt` is needed for the upstream asset tools.
- ROS 2, `ros_gz_bridge`, `ros_gz_image`, `numpy`, and OpenCV only when using
  `GazeboAdapter` against live Gazebo topics. They are not required to start a
  world or run mock-SDK tests.
- Docker users need the same packages in the container. GUI mode additionally
  needs an X11/Wayland socket, matching X authority, and a usable GPU/OpenGL
  driver. Headless camera/lidar mode uses `--headless-rendering`.

## Install from a fresh checkout

```bash
git clone <your-nimbus-repository-url>
cd nimbus/simulation/hospital_world
ign gazebo --versions                 # must show a 6.x version
python3 -m pip install -r requirements.txt
```

All hospital and robot runtime assets are already local. Do not run the Fuel
vendor tool during normal use. If a shell needs to resolve the models manually:

```bash
export IGN_GAZEBO_RESOURCE_PATH="$PWD/models:$PWD/models/robots:$PWD/fuel_models"
export GZ_SIM_RESOURCE_PATH="$IGN_GAZEBO_RESOURCE_PATH"
```

Set `IGN_PARTITION` to an identical value in every terminal that should share a
simulation. Set different values to isolate concurrent simulations.

## Run the simulation

From the project root:

```bash
# Hospital only (default view omits the ceiling visual; ceiling collision remains)
./launch_gui.sh
./launch_headless.sh
# Interior two-RACECAR view: hides only the ceiling visual and retains its collision
./launch_interior_gui.sh

# One robot: robot_1 at (0, 12, 0), namespace /robot_1
./launch_hospital_one_racecar.sh
HEADLESS=1 ./launch_hospital_one_racecar.sh

# Two robots: robot_1 at (0, 12, 0), robot_2 at (0, 9.5, 0)
./launch_hospital_two_racecars.sh
HEADLESS=1 ./launch_hospital_two_racecars.sh
```

The launch scripts use the dedicated Ignition transport partition
`custom_hospital_world` by default, preventing a stale server on the default
partition from taking over the GUI.  Use the same partition for commands in a
second terminal:

```bash
export IGN_PARTITION=custom_hospital_world
ign model -m robot_1 -p
ign model -m robot_2 -p
```

To run an intentionally separate hospital instance, set
`HOSPITAL_IGN_PARTITION` before launching.

`hospital.sdf` omits the full pale ceiling visual by default while retaining
the original ceiling collision mesh. `launch_interior_gui.sh` additionally
uses lower, slightly warm lighting, then generates the same two
wrapper-compatible RACECAR instances as `launch_hospital_two_racecars.sh`.
Robot collision behaviour is unchanged.

Extra Fortress options pass through the launchers. For example,
`HEADLESS=1 ./launch_hospital_two_racecars.sh --iterations 1000` is a bounded
headless run. The single and two-robot worlds are generated locally from
`hospital.sdf` and `models/robots/mit_racecar/model.sdf`; generated SDFs are
not source-of-truth files.

Validate before deployment:

```bash
./validate_hospital.sh
./validate_racecars.sh
python3 scripts/validate_topology.py
python3 -m unittest discover -s tests -v
python3 -m unittest discover -s tests/ground_robot_sdk -v
```

`validate_racecars.sh` starts a temporary Fortress server, verifies both
namespaces and all sensor streams, and sends a brief 0.10 m/s command to
`robot_1`; do not run it while another process owns the same `IGN_PARTITION`.

## Hospital world

`hospital.sdf` is the Fortress conversion of the upstream single-floor
hospital. The original Gazebo Classic worlds remain in `worlds/` for
provenance, but are not used at runtime. The scene includes a north elevator
lobby, a central north-south circulation spine, east and west room bands,
waiting/seating approaches, a nurses-station approach, side corridors, and
south equipment/room approaches. Collision comes from the original walls plus
the placed asset collision geometry.

The converted world fixes three malformed upstream object heights only in the
Fortress output. The original source asset files and licenses remain intact.
Known limitations: this deployment maps the first floor only, has no dynamic
door control or pedestrian behaviour, and its semantic room labels are kept
conservative when an asset does not establish a clinical use.

## RACECAR model and interfaces

The vendored model lives at `models/robots/mit_racecar/`; its mesh provenance
and redistribution caveat are in `ASSET_ATTRIBUTION.md`. The Fortress model
uses a 0.325 m wheelbase, 0.20 m track, 0.05 m wheel radius, 4.0 kg chassis,
front steering limit of 0.55 rad, and a conservative 0.405 x 0.20 m chassis
collision box. It preserves the existing Ackermann controller rather than
introducing a new robot controller.

For a robot named `robot_1`, the live Gazebo interfaces are:

| Interface | Topic / frame |
| --- | --- |
| Ackermann velocity input | `/robot_1/cmd_vel` |
| Odometry | `/robot_1/odom` |
| Transform | `/robot_1/tf`, `robot_1/odom` -> `robot_1/base_link` |
| RGB camera | `/robot_1/camera/image` (640x480, 2 Hz) |
| Front range | `/robot_1/ultrasonic/scan` (five rays, 15 Hz) |
| Ground-truth model poses | `/world/hospital/pose/info` |

`robot_2` has the same interfaces under `/robot_2`. Existing ROS 2 clients can
bridge them through the SDK’s `GazeboAdapter`; the wrappers start
`ros_gz_bridge` and `ros_gz_image` when a `RobotRuntime` starts.
The SDF pose publisher uses `ignition.msgs.Pose_V`; the adapter bridges it to
`tf2_msgs/msg/TFMessage` and stores the selected model pose separately from
odometry as Gazebo world-frame ground truth.

## Python SDK

Start Gazebo first, then in another terminal using the same `IGN_PARTITION`:

```bash
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"
python3 run_robot_control.py --robot-name robot_1 --namespace /robot_1 --port 8081
```

This standalone HTTP wrapper requires fresh odometry and range feedback for
navigation. Add `--validation-source ground_truth` to require Gazebo pose
verification as well. The Edge Supervisor does not call this HTTP server; it
constructs the same `RobotRuntime` directly.

For the complete Edge integration, start the six-robot world and edge in
separate terminals with the same partition:

```bash
HOSPITAL_IGN_PARTITION=custom_hospital_world ./launch_hospital_six_racecars.sh
HOSPITAL_IGN_PARTITION=custom_hospital_world python3 ../../edge/run_supervisor.py \
  --cluster east-wing --robot-backend sim --gazebo-validation ground_truth
```

Before submitting a cloud task, confirm that the edge startup table reports
`READY`. Useful transport checks are:

```bash
ign topic -e -n 1 -t /robot_1/odom
ign topic -e -n 1 -t /robot_1/ultrasonic/scan
ign topic -e -n 1 -t /world/hospital/pose/info
```

The live validation harness sends one bounded navigation request and exits
non-zero if readiness, the expected terminal status, or arrival verification
fails:

```bash
python3 scripts/validate_edge_gazebo.py --robot R1 --target 1.0 12.0 \
  --validation-source ground_truth --expect COMPLETED
```

The harness can terminate only its ultrasonic bridge after motion starts. It
must end with `SENSOR_UNAVAILABLE`, never `COMPLETED`:

```bash
python3 scripts/validate_edge_gazebo.py --robot R1 --target 2.0 12.0 \
  --validation-source ground_truth --disconnect range \
  --disconnect-after 0.5 --expect SENSOR_UNAVAILABLE
```

For a fresh simulator session, this creates a box 18.5 cm ahead of R1's front
sensor and verifies a real `BLOCKED` result:

```bash
ign service -s /world/hospital/create --reqtype ignition.msgs.EntityFactory \
  --reptype ignition.msgs.Boolean --timeout 5000 \
  --req 'sdf: "<sdf version=\"1.9\"><model name=\"nimbus_validation_obstacle\"><static>true</static><pose>0.6 12 0.3 0 0 0</pose><link name=\"body\"><collision name=\"collision\"><geometry><box><size>0.3 1.0 0.6</size></box></geometry></collision><visual name=\"visual\"><geometry><box><size>0.3 1.0 0.6</size></box></geometry></visual></link></model></sdf>"'
python3 scripts/validate_edge_gazebo.py --robot R1 --target 1.0 12.0 \
  --validation-source ground_truth --expect BLOCKED
```

Deterministic sensor-loss, localization-loss, obstacle, and
ground-truth-divergence tests can
be run without claiming live Gazebo execution:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=. python3 -m pytest -q \
  ../../edge/tests/test_edge_gazebo_regressions.py
```

Use the high-level facade from an application. For a Gazebo world-frame target,
resolve it through the topology helper first: the SDK's `frame="world"` is the
robot's startup-local odometry frame rather than Gazebo's global world frame.

```python
from ground_robot_sdk import RobotRuntime
from topology.resolver import resolve_for_robot

with RobotRuntime(robot_name="robot_1", namespace="/robot_1") as robot:
    goal = resolve_for_robot("2B-12", "robot_1")
    task = robot.commands.goto(*goal["sdk_startup_waypoint"], frame="world", speed=0.30)
    robot.commands.wait(1.0)
    frame = robot.commands.photo()       # latest CameraFrame, never a fabricated image
    notification = robot.commands.beep() # completed notification; the model has no audio actuator
    robot.commands.stop()
    report = robot.commands.report()     # state + capabilities, JSON-serializable
```

`goto(x, y, frame="world")` uses the runtime's startup odometry frame;
`frame="base_link"` interprets `(x, y)` as a relative target. Navigation is
an odometry-driven point-to-point controller, not a global planner or dynamic
obstacle-avoidance system. `photo()` returns the latest frame from the camera
topic and may be unavailable before its first message. `beep()` deliberately
does not claim to play sound because the physical model has no speaker; its
returned command is suitable for a UI or external notification bridge.

For two clients, use distinct name/namespace/port values:

```python
robot_1 = RobotRuntime(robot_name="robot_1", namespace="/robot_1")
robot_2 = RobotRuntime(robot_name="robot_2", namespace="/robot_2")
```

## Topology and spawn locations

`topology/hospital_topology.json` is the `topo-v0` graph and
`topology/topology_waypoints.json` maps every node to `[x, y]` metres in the
Gazebo `world` frame. It has 13 nodes, 15 bidirectional edges, and three
physical groups: `east`, `west`, and `shared`. Weights are straight-line
world-frame metres. The labelled overlay is
`topology/hospital_topology.png`.

The graph is generated from the first-floor wall collision mesh and placed
assets. Node `wing` matches its sole group. `lift_lobby`, `west_north_gate`,
and `east_north_gate` are exclusive scheduler gates with independent
reservation IDs; this is not physical door actuation. The logical PRD aliases
for Ward 2B, Bed 2B-12, Ward 2C, Ward 3, the equipment store, and nurse station
live in `topology/scenario_locations.json` and map to physical nodes without
renaming those nodes or inventing bed coordinates.

`robot_1` starts exactly at shared `north_hall` (`[0, 12, 0]`). `robot_2`
starts on the shared `north_hall`–`nurse_station_approach` edge (`[0, 9.5, 0]`),
so it intentionally has no arbitrary start node. See
[topology/README.md](topology/README.md) for all node, edge, mapping,
coordinate-conversion, gate, and route-planning details.

```bash
python3 scripts/generate_topology.py
python3 scripts/validate_topology.py
```

Topology validation checks `topo-v0` structure, groups/wings, PRD mappings,
exclusive-gate reservation metadata, robot start association, waypoint units,
edge costs, connectivity, and Fortress collision contacts. It tests 13
0.65 x 0.65 m junction envelopes, 15 full swept 0.65 x 0.35 m RACECAR
envelopes, and both spawn envelopes. Revalidate with a larger robot footprint
or a controller requiring a different turn radius.

## Project layout

```text
hospital_world/
├── hospital.sdf                 Fortress hospital source of truth
├── models/                      Hospital models and robots/mit_racecar
├── fuel_models/                 Offline third-party hospital assets + manifest
├── ground_robot_sdk/            Namespaced Python control SDK
├── topology/                    topo-v0 graph, PRD aliases, resolver, PNG
├── config/robot_deployment.json two-robot spawn/start configuration
├── tools/                       deterministic world/model utilities
├── scripts/                     topology generation and validation
├── tests/ground_robot_sdk/      offline SDK test suite
├── launch_*.sh                  GUI, headless, one/two robot entry points
└── LICENSE, NOTICE              upstream licensing and notices
```

## Troubleshooting

- **Missing mesh or model URI:** run launchers from this directory, or export
  both resource variables shown above. Ensure `fuel_models/` and
  `models/robots/mit_racecar/` were included in the checkout.
- **GUI does not open in Docker:** use a display socket/X authority/GPU setup,
  or use `HEADLESS=1` with a robot launcher. A stale `DISPLAY` can be avoided
  by the headless launchers automatically.
- **No ROS messages:** start the relevant robot world first; source your ROS 2
  installation; use matching `IGN_PARTITION`; and ensure `ros_gz_bridge` plus
  `ros_gz_image` are installed.
- **Robot does not move:** publish to the instance namespace (for example,
  `/robot_1/cmd_vel`), not the legacy global `/cmd_vel`. Check the range guard
  configuration before commanding forward motion.
- **Camera/lidar unavailable headlessly:** use `HEADLESS=1` so Fortress starts
  its off-screen rendering path; the host still needs a compatible EGL/OpenGL
  implementation for GPU sensors.

## Attribution and licensing

The hospital is derived from `ZimaUSP/d_hospital_world`; preserve this
repository's `LICENSE`, `NOTICE`, upstream source files, and Fuel model
metadata in `fuel_models/MANIFEST.json`. Fuel models have their own licenses
and metadata. The RACECAR visual mesh provenance and the upstream license
limitation are documented in `models/robots/mit_racecar/ASSET_ATTRIBUTION.md`.
No automatic Git commit or publication is performed by this project.
