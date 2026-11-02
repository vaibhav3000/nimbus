"""Resolve logical PRD destinations to physical Gazebo topology waypoints.

The SDK's ``goto(..., frame='world')`` uses its startup-local odometry frame,
not Gazebo's global world frame. Use :func:`world_to_sdk_startup` with a
robot's configured global spawn pose before passing a resolved waypoint to it.
"""
from __future__ import annotations

import json
import heapq
from math import cos, sin
from pathlib import Path


ROOT = Path(__file__).resolve().parent
TOPOLOGY_PATH = ROOT / "hospital_topology.json"
WAYPOINTS_PATH = ROOT / "topology_waypoints.json"
SCENARIOS_PATH = ROOT / "scenario_locations.json"
DEPLOYMENT_PATH = ROOT.parent / "config" / "robot_deployment.json"


class UnknownDestination(ValueError):
    """Raised when a PRD destination identifier cannot be resolved."""


def _read(path: Path):
    return json.loads(path.read_text())


def _normalise(identifier: str) -> str:
    if not isinstance(identifier, str) or not identifier.strip():
        raise UnknownDestination("destination identifier must be a non-empty string")
    return " ".join(identifier.lower().strip().replace("_", "-").split())


def load_data():
    """Load topology, waypoints, and logical PRD aliases without Gazebo."""
    return _read(TOPOLOGY_PATH), _read(WAYPOINTS_PATH), _read(SCENARIOS_PATH)


def resolve_destination(identifier: str) -> dict:
    """Return logical mapping metadata plus physical node and ``[x, y]`` world waypoint."""
    topology, waypoint_document, scenarios = load_data()
    requested = _normalise(identifier)
    for stable_id, location in scenarios["locations"].items():
        identifiers = [stable_id, *location.get("aliases", [])]
        if requested in {_normalise(value) for value in identifiers}:
            node = location["physical_node"]
            if node not in topology["nodes"] or node not in waypoint_document["waypoints"]:
                raise RuntimeError(f"scenario location {stable_id} references unresolved physical node {node}")
            return {
                "identifier": stable_id,
                "physical_node": node,
                "waypoint": list(waypoint_document["waypoints"][node]),
                "frame": waypoint_document["frame"],
                "units": waypoint_document["units"],
                "logical_kind": location["logical_kind"],
                "metadata": dict(location.get("metadata", {})),
                **({"logical_parent": location["logical_parent"]} if "logical_parent" in location else {}),
            }
    known = ", ".join(sorted(scenarios["locations"]))
    raise UnknownDestination(f"unknown destination {identifier!r}; expected an alias for: {known}")


def world_to_sdk_startup(world_xy, startup_world_pose) -> tuple[float, float]:
    """Transform a Gazebo world point into the SDK's startup-local frame.

    ``startup_world_pose`` is ``(x, y, yaw)`` in Gazebo world metres/radians.
    The formula mirrors ``NavigationController._on_odometry`` exactly.
    """
    if len(world_xy) != 2 or len(startup_world_pose) != 3:
        raise ValueError("world_xy must be [x, y] and startup_world_pose must be [x, y, yaw]")
    target_x, target_y = map(float, world_xy)
    origin_x, origin_y, yaw = map(float, startup_world_pose)
    dx, dy = target_x - origin_x, target_y - origin_y
    return cos(yaw) * dx + sin(yaw) * dy, -sin(yaw) * dx + cos(yaw) * dy


def resolve_for_robot(identifier: str, robot_id: str) -> dict:
    """Resolve a PRD alias and add the SDK-local goal for a deployed robot."""
    deployment = _read(DEPLOYMENT_PATH)
    try:
        robot = deployment["robots"][robot_id]
    except KeyError as error:
        raise ValueError(f"unknown deployed robot {robot_id!r}") from error
    result = resolve_destination(identifier)
    result["sdk_startup_waypoint"] = list(world_to_sdk_startup(result["waypoint"], robot["spawn_pose"]))
    result["robot_id"] = robot_id
    result["namespace"] = robot["namespace"]
    return result


def shortest_route(start_node: str, destination_identifier: str) -> dict:
    """Return a bidirectional Dijkstra route to a logical PRD destination.

    Costs are the topology's documented straight-line metre weights. The
    result is a graph route only; it is not an obstacle-aware trajectory.
    """
    topology, _, _ = load_data()
    if start_node not in topology["nodes"]:
        raise ValueError(f"unknown physical start node {start_node!r}")
    destination = resolve_destination(destination_identifier)
    target = destination["physical_node"]
    adjacency = {node: [] for node in topology["nodes"]}
    for source, target_node, cost in topology["edges"]:
        adjacency[source].append((target_node, float(cost)))
        adjacency[target_node].append((source, float(cost)))
    queue = [(0.0, start_node, [start_node])]
    visited = set()
    while queue:
        cost, node, path = heapq.heappop(queue)
        if node in visited:
            continue
        visited.add(node)
        if node == target:
            return {"nodes": path, "cost_m": cost, "destination": destination}
        for neighbour, edge_cost in adjacency[node]:
            if neighbour not in visited:
                heapq.heappush(queue, (cost + edge_cost, neighbour, path + [neighbour]))
    raise RuntimeError(f"no topology route from {start_node} to {target}")
