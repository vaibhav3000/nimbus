# MIT RACECAR port attribution

This Fortress-native model is a port, not an import of the upstream ROS /
Gazebo stack. Its visual assets and reference dimensions come from the
official MIT RACECAR repository:

- Repository: <https://github.com/mit-racecar/racecar_gazebo>
- Description path: `racecar_description/urdf/{racecar,macros}.xacro`
- Mesh path: `racecar_description/meshes/`
- Commit inspected: `effd29f9edc20a03c8a1f666fb6fb6c0cbbee8ff`

The upstream checkout at that commit has no top-level `LICENSE` or `COPYING`,
and `racecar_description/package.xml` declares its license as `TODO`.
Accordingly, this file records provenance and does **not** claim a license for
the upstream assets. Only the eight visual meshes needed by this model were
copied. No ROS launch files, plugins, controllers, or upstream Gazebo stack
are included or invoked.

The model's chassis mass/inertia, wheel mass/inertia, 0.325 m wheelbase,
0.20 m track, and 0.05 m wheel radius are adapted from those description
files. The Fortress SDF, sensors, and Ackermann plugin configuration are
project-native.
