#!/usr/bin/env python3
"""Validate topo-v0 data and test its robot envelope in Gazebo Fortress.

Each graph edge is represented by one swept, yaw-aligned box.  Its length is
the edge length plus the 0.65 m RACECAR envelope length, therefore any static
obstacle anywhere on that direct segment produces a Fortress contact.  A
0.65 m square is also placed at each node to check the junction/steering
envelope.  The tests intentionally exclude only the two supporting floors.
"""
import argparse
import json
from math import atan2, hypot, isfinite
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
TOPOLOGY = ROOT / "topology" / "hospital_topology.json"
WAYPOINTS = ROOT / "topology" / "topology_waypoints.json"
SCENARIOS = ROOT / "topology" / "scenario_locations.json"
DEPLOYMENT = ROOT / "config" / "robot_deployment.json"
REQUIRED_KEYS = {"version", "note", "nodes", "edges", "groups"}
VALID_KINDS = {"corridor", "gate", "zone", "store", "ward"}
EXPECTED_GROUPS = {"east", "west", "shared"}
EXPECTED_EXCLUSIVE_GATES = {"lift_lobby", "west_north_gate", "east_north_gate"}
SUPPORT_COLLISIONS = {
    "ground_plane::link::collision",
    "aws_robomaker_hospital_floor_01_floor_1::link::collision",
}
CHASSIS_LENGTH = 0.65
CHASSIS_WIDTH = 0.35
JUNCTION_SIDE = 0.65


def fail(message):
    raise ValueError(message)


def number(value, context):
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not isfinite(value):
        fail(f"{context} must be a finite number")
    return float(value)


def load_and_check_schema():
    """Return checked topology and waypoint data without touching Gazebo."""
    try:
        topology = json.loads(TOPOLOGY.read_text())
        waypoint_document = json.loads(WAYPOINTS.read_text())
        scenarios = json.loads(SCENARIOS.read_text())
        deployment = json.loads(DEPLOYMENT.read_text())
    except (OSError, json.JSONDecodeError) as error:
        fail(f"unable to read topology JSON: {error}")
    if set(topology) != REQUIRED_KEYS:
        fail(f"topology root keys must be exactly {sorted(REQUIRED_KEYS)}")
    if topology["version"] != "topo-v0" or not isinstance(topology["note"], str):
        fail("topology must have version topo-v0 and a string note")
    nodes = topology["nodes"]
    if not isinstance(nodes, dict) or not nodes:
        fail("nodes must be a non-empty dictionary")
    for node_id, attributes in nodes.items():
        if not isinstance(node_id, str) or not node_id or not isinstance(attributes, dict):
            fail("each node must have a non-empty string ID and attribute dictionary")
        if not isinstance(attributes.get("kind"), str) or attributes["kind"] not in VALID_KINDS:
            fail(f"node {node_id} has unsupported or missing kind")
        if not isinstance(attributes.get("area"), str) or not attributes["area"]:
            fail(f"node {node_id} needs a non-empty area")
        if attributes.get("wing") not in EXPECTED_GROUPS:
            fail(f"node {node_id} needs wing east, west, or shared")
        if "beds" in attributes and (not isinstance(attributes["beds"], int) or isinstance(attributes["beds"], bool) or attributes["beds"] < 0):
            fail(f"node {node_id} beds must be a non-negative integer")
        if "paediatric" in attributes and not isinstance(attributes["paediatric"], bool):
            fail(f"node {node_id} paediatric must be boolean")
        if "speed_cap" in attributes and number(attributes["speed_cap"], f"node {node_id} speed_cap") <= 0:
            fail(f"node {node_id} speed_cap must be positive")
        if "exclusive" in attributes and not isinstance(attributes["exclusive"], bool):
            fail(f"node {node_id} exclusive must be boolean")
    if not isinstance(waypoint_document, dict) or set(waypoint_document) != {"frame", "units", "waypoints"}:
        fail("waypoint JSON must contain exactly frame, units, and waypoints")
    if waypoint_document["frame"] != "world" or waypoint_document["units"] != "m":
        fail("waypoints must use the Gazebo world frame in metres")
    waypoints = waypoint_document["waypoints"]
    if set(waypoints) != set(nodes):
        fail("waypoint IDs must exactly match topology node IDs")
    for node_id, coordinate in waypoints.items():
        if not isinstance(coordinate, list) or len(coordinate) != 2:
            fail(f"waypoint {node_id} must be a [x, y] pair")
        number(coordinate[0], f"waypoint {node_id} x")
        number(coordinate[1], f"waypoint {node_id} y")
    edges = topology["edges"]
    if not isinstance(edges, list) or not edges:
        fail("edges must be a non-empty list")
    adjacency = {node_id: set() for node_id in nodes}
    seen_edges = set()
    for edge in edges:
        if not isinstance(edge, list) or len(edge) != 3:
            fail("every edge must be [source, destination, weight]")
        source, destination, weight = edge
        if source not in nodes or destination not in nodes or source == destination:
            fail(f"invalid edge endpoints: {edge[:2]}")
        unordered = tuple(sorted((source, destination)))
        if unordered in seen_edges:
            fail(f"duplicate (including reverse duplicate) edge: {edge[:2]}")
        seen_edges.add(unordered)
        actual = hypot(waypoints[source][0] - waypoints[destination][0],
                       waypoints[source][1] - waypoints[destination][1])
        if abs(number(weight, f"edge {source}->{destination} weight") - actual) > 0.011:
            fail(f"edge {source}->{destination} weight does not match its documented straight-line metre cost")
        adjacency[source].add(destination)
        adjacency[destination].add(source)  # topo-v0 convention here is bidirectional.
    visited, frontier = set(), [next(iter(nodes))]
    while frontier:
        current = frontier.pop()
        if current in visited:
            continue
        visited.add(current)
        frontier.extend(adjacency[current] - visited)
    if visited != set(nodes):
        fail(f"graph is disconnected; unreachable: {sorted(set(nodes) - visited)}")
    groups = topology["groups"]
    if not isinstance(groups, dict) or set(groups) != EXPECTED_GROUPS:
        fail(f"groups must be exactly {sorted(EXPECTED_GROUPS)}")
    membership = []
    for name, area in groups.items():
        if not isinstance(name, str) or not name or not isinstance(area, list) or not area:
            fail("each group needs a non-empty name and node list")
        if any(node not in nodes for node in area):
            fail(f"group {name} references an unknown node")
        if any(nodes[node]["wing"] != name for node in area):
            fail(f"group {name} disagrees with node wing metadata")
        membership.extend(area)
    if len(membership) != len(set(membership)) or set(membership) != set(nodes):
        fail("groups must provide one complete, non-overlapping node partition")
    exclusive = {node for node, attributes in nodes.items() if attributes.get("exclusive")}
    if exclusive != EXPECTED_EXCLUSIVE_GATES:
        fail(f"exclusive gates must be exactly {sorted(EXPECTED_EXCLUSIVE_GATES)}")
    if any(nodes[node]["kind"] != "gate" for node in exclusive):
        fail("exclusive metadata can only be assigned to gate nodes")
    reservation_ids = {nodes[node].get("reservation_id") for node in exclusive}
    if None in reservation_ids or len(reservation_ids) != len(exclusive):
        fail("each exclusive gate needs a distinct reservation_id")
    check_scenarios(scenarios, nodes, waypoints)
    check_deployment(deployment, nodes, waypoints, seen_edges)
    return topology, waypoints, deployment


def check_scenarios(scenarios, nodes, waypoints):
    if not isinstance(scenarios, dict) or scenarios.get("version") != "scenario-locations-v1":
        fail("scenario locations must use version scenario-locations-v1")
    locations = scenarios.get("locations")
    expected = {
        "ward-2b": "east_waiting_approach", "bed-2b-12": "east_waiting_approach",
        "ward-2c": "west_waiting_approach", "ward-3": "south_room_approach",
        "equipment-store": "south_equipment_approach", "nurse-station": "nurse_station_approach",
    }
    if not isinstance(locations, dict) or set(locations) != set(expected):
        fail("scenario locations must contain the six documented stable identifiers")
    aliases = set()
    for identifier, node in expected.items():
        location = locations[identifier]
        if location.get("physical_node") != node or node not in nodes or node not in waypoints:
            fail(f"scenario location {identifier} must resolve to {node}")
        if not isinstance(location.get("aliases"), list) or not location["aliases"]:
            fail(f"scenario location {identifier} needs aliases")
        for alias in [identifier, *location["aliases"]]:
            normalized = " ".join(alias.lower().strip().replace("_", "-").split())
            if normalized in aliases:
                fail(f"scenario alias {alias!r} is duplicated")
            aliases.add(normalized)
    if not locations["ward-2b"].get("metadata", {}).get("paediatric"):
        fail("ward-2b must retain paediatric PRD metadata")
    if not locations["ward-2c"].get("metadata", {}).get("isolation"):
        fail("ward-2c must retain isolation PRD metadata")
    if locations["bed-2b-12"].get("logical_parent") != "ward-2b":
        fail("bed-2b-12 must identify ward-2b as its logical parent")


def check_deployment(deployment, nodes, waypoints, edge_set):
    if not isinstance(deployment, dict) or deployment.get("version") != "robot-deployment-v1":
        fail("robot deployment must use version robot-deployment-v1")
    robots = deployment.get("robots")
    if not isinstance(robots, dict) or not {"robot_1", "robot_2"}.issubset(robots):
        fail("robot deployment must describe robot_1 and robot_2")
    namespaces = set()
    for robot_id, robot in robots.items():
        namespace = robot.get("namespace")
        if namespace != f"/{robot_id}" or namespace in namespaces:
            fail(f"{robot_id} needs unique namespace /{robot_id}")
        namespaces.add(namespace)
        pose = robot.get("spawn_pose")
        if not isinstance(pose, list) or len(pose) != 3:
            fail(f"{robot_id} spawn_pose must be [x, y, yaw]")
        x, y, yaw = (number(value, f"{robot_id} spawn pose") for value in pose)
        if robot.get("wing") not in EXPECTED_GROUPS:
            fail(f"{robot_id} must have east, west, or shared wing")
        node = robot.get("start_node")
        edge = robot.get("start_edge")
        if node is not None:
            if node not in nodes or edge is not None:
                fail(f"{robot_id} start_node must be a node with no start_edge")
            if hypot(x - waypoints[node][0], y - waypoints[node][1]) > 0.01:
                fail(f"{robot_id} spawn does not coincide with start_node {node}")
            if nodes[node]["wing"] != robot["wing"]:
                fail(f"{robot_id} start_node wing disagrees with deployment wing")
        else:
            if not isinstance(edge, list) or len(edge) != 2 or tuple(sorted(edge)) not in edge_set:
                fail(f"{robot_id} without a start_node needs a valid start_edge")
            (ax, ay), (bx, by) = waypoints[edge[0]], waypoints[edge[1]]
            length = hypot(bx - ax, by - ay)
            projection = ((x - ax) * (bx - ax) + (y - ay) * (by - ay)) / (length * length)
            distance = abs((bx - ax) * (ay - y) - (ax - x) * (by - ay)) / length
            if not 0 <= projection <= 1 or distance > 0.01:
                fail(f"{robot_id} spawn is not on documented start_edge")
            if any(nodes[endpoint]["wing"] != robot["wing"] for endpoint in edge):
                fail(f"{robot_id} start_edge wing disagrees with deployment wing")


def element(tag, text=None, **attrs):
    value = ET.Element(tag, attrs)
    if text is not None:
        value.text = text
    return value


def contact_probe(name, x, y, yaw, length, width):
    model = element("model", name=f"topology_probe_{name}")
    model.append(element("pose", f"{x} {y} 0.30 0 0 {yaw}"))
    link = ET.SubElement(model, "link", {"name": "link"})
    link.append(element("gravity", "false"))
    collision = ET.SubElement(link, "collision", {"name": "footprint"})
    geometry = ET.SubElement(collision, "geometry")
    box = ET.SubElement(geometry, "box")
    box.append(element("size", f"{length} {width} 0.50"))
    sensor = ET.SubElement(link, "sensor", {"name": "contact", "type": "contact"})
    contact = ET.SubElement(sensor, "contact")
    contact.append(element("collision", "footprint"))
    contact.append(element("topic", f"/hospital_topology/{name}"))
    sensor.append(element("always_on", "true"))
    sensor.append(element("update_rate", "30"))
    return model


def build_probes(topology, waypoints, deployment):
    probes = {}
    for node, (x, y) in waypoints.items():
        probes[f"node_{node}"] = (x, y, 0.0, JUNCTION_SIDE, JUNCTION_SIDE)
    for index, (source, destination, weight) in enumerate(topology["edges"]):
        sx, sy = waypoints[source]
        dx, dy = waypoints[destination]
        probes[f"edge_{index:02d}_{source}_to_{destination}"] = (
            (sx + dx) / 2, (sy + dy) / 2, atan2(dy - sy, dx - sx), float(weight) + CHASSIS_LENGTH, CHASSIS_WIDTH)
    for robot_id, robot in deployment["robots"].items():
        x, y, yaw = robot["spawn_pose"]
        probes[f"spawn_{robot_id}"] = (x, y, yaw, CHASSIS_LENGTH, CHASSIS_WIDTH)
    return probes


def ensure_ign():
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(folder) / "ign"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return
    fail("ign executable is unavailable; Gazebo Fortress is required for physical validation")


def run_physics_validation(topology, waypoints, deployment):
    ensure_ign()
    probes = build_probes(topology, waypoints, deployment)
    tree = ET.parse(ROOT / "hospital.sdf")
    world = tree.getroot().find("world")
    world.append(element("plugin", filename="ignition-gazebo-contact-system",
                         name="ignition::gazebo::systems::Contact"))
    for name, values in probes.items():
        world.append(contact_probe(name, *values))
    ET.indent(tree, space="  ")
    with tempfile.TemporaryDirectory(prefix="hospital_topology_check_") as directory:
        world_path = Path(directory) / "topology_check.sdf"
        tree.write(world_path, encoding="utf-8", xml_declaration=True)
        environment = os.environ.copy()
        local_paths = f"{ROOT / 'models'}:{ROOT / 'models/robots'}:{ROOT / 'fuel_models'}"
        environment["IGN_GAZEBO_RESOURCE_PATH"] = local_paths + (
            ":" + environment["IGN_GAZEBO_RESOURCE_PATH"] if environment.get("IGN_GAZEBO_RESOURCE_PATH") else "")
        environment["IGN_PARTITION"] = f"hospital_topology_check_{os.getpid()}"
        server = subprocess.Popen(["ign", "gazebo", "-r", "-s", str(world_path)],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=environment)
        try:
            time.sleep(6)
            failures = {}
            for name in probes:
                try:
                    result = subprocess.run(["ign", "topic", "-e", "-n", "1", "-t", f"/hospital_topology/{name}"],
                                            env=environment, text=True, stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, timeout=3)
                except subprocess.TimeoutExpired:
                    failures[name] = "contact sensor published no result"
                    continue
                names = set(re.findall(r'name: "([^"]+)"', result.stdout))
                obstacles = sorted(value for value in names
                                   if not value.startswith(f"topology_probe_{name}::")
                                   and value not in SUPPORT_COLLISIONS)
                if obstacles:
                    failures[name] = ", ".join(obstacles)
            if failures:
                for name, details in failures.items():
                    print(f"FAIL physics {name}: {details}", file=sys.stderr)
                return False, len(probes)
            return True, len(probes)
        finally:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-physics", action="store_true", help="run JSON/schema checks only")
    args = parser.parse_args()
    try:
        topology, waypoints, deployment = load_and_check_schema()
        print(f"PASS schema: {len(topology['nodes'])} nodes, {len(topology['edges'])} bidirectional edges, "
              f"{len(topology['groups'])} groups, 6 PRD locations, and {len(deployment['robots'])} robot deployments; connected graph.")
        if not args.no_physics:
            passed, probes = run_physics_validation(topology, waypoints, deployment)
            if not passed:
                return 1
            print(f"PASS physics: {probes} Fortress contact probes found no non-floor obstacle collisions "
                  f"({len(waypoints)} junction envelopes, {len(topology['edges'])} swept edge envelopes, "
                  f"{len(deployment['robots'])} robot spawn envelopes).")
    except (ValueError, ET.ParseError, OSError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
