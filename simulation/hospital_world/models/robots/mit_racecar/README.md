# Fortress MIT RACECAR port

This is a project-native Ignition Gazebo Fortress SDF port of the official MIT
RACECAR description. It does not invoke the upstream ROS / Gazebo stack.

## Geometry and kinematics

| Property | Value |
| --- | --- |
| Rear axle reference | model origin |
| Wheelbase | 0.325 m |
| Track / kingpin width | 0.20 m |
| Wheel radius / width | 0.05 m / 0.045 m |
| Chassis mass | 4.0 kg |
| Wheel mass | 0.34055 kg each |
| Steering | two front kingpin joints, Fortress Ackermann plugin |

The body, four wheels, steering hinges, and Hokuyo housing are copied only as
visual meshes. The SDF defines Fortress-native joints, collision primitives,
inertias, sensors, and controllers.

## Stable interfaces

The model keeps the project's existing contract:

```text
/cmd_vel                       ignition.msgs.Twist
/odom                          ignition.msgs.Odometry
/picarx/camera/image           ignition.msgs.Image
/picarx/camera/camera_info     ignition.msgs.CameraInfo
/picarx/ultrasonic/scan        ignition.msgs.LaserScan
```

`linear.x` drives the vehicle; `angular.z` commands Ackermann steering.

## Sensors

- RGB camera: source RACECAR front-camera area, `x=0.390 m`, `z=0.040 m`
  relative to the deck (`base_link`), 640×480 RGB at 15 Hz.
- Narrow range sensor: source Hokuyo area, `x=0.265 m`, `z=0.075 m`
  relative to the deck, GPU lidar with five rays across ±0.10 rad at 15 Hz.

See [ASSET_ATTRIBUTION.md](ASSET_ATTRIBUTION.md) for source provenance and the
upstream licensing limitation.
