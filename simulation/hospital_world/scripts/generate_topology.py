#!/usr/bin/env python3
"""Generate the topo-v0 hospital graph, waypoints, and labelled floor plan."""
import json
from math import hypot
from pathlib import Path
import xml.etree.ElementTree as ET

import matplotlib.pyplot as plt


ROOT = Path(__file__).resolve().parents[1]
TOPOLOGY_DIR = ROOT / "topology"

# These labels are constrained by observed models / floor geometry.  Descriptive
# labels ending in "approach" are navigation waypoints, not asserted room names.
NODES = {
    "lift_lobby": {"kind": "gate", "area": "north elevator portal lobby", "wing": "shared", "speed_cap": 0.35, "exclusive": True, "reservation_id": "gate:lift_lobby"},
    "north_hall": {"kind": "corridor", "area": "north central corridor", "wing": "shared", "speed_cap": 0.45},
    "west_north_gate": {"kind": "gate", "area": "west north room-band doorway", "wing": "west", "speed_cap": 0.30, "exclusive": True, "reservation_id": "gate:west_north_gate"},
    "east_north_gate": {"kind": "gate", "area": "east north room-band doorway", "wing": "east", "speed_cap": 0.30, "exclusive": True, "reservation_id": "gate:east_north_gate"},
    "west_waiting_approach": {"kind": "zone", "area": "west waiting seating approach", "wing": "west", "speed_cap": 0.30},
    "east_waiting_approach": {"kind": "zone", "area": "east waiting seating approach", "wing": "east", "speed_cap": 0.30},
    "west_mid_hall": {"kind": "corridor", "area": "west central bypass", "wing": "west", "speed_cap": 0.40},
    "east_mid_hall": {"kind": "corridor", "area": "east central bypass", "wing": "east", "speed_cap": 0.40},
    "nurse_station_approach": {"kind": "zone", "area": "nurse-station approach", "wing": "shared", "speed_cap": 0.25},
    "west_south_hall": {"kind": "corridor", "area": "west south corridor", "wing": "west", "speed_cap": 0.40},
    "east_south_hall": {"kind": "corridor", "area": "east south corridor", "wing": "east", "speed_cap": 0.40},
    "south_equipment_approach": {"kind": "zone", "area": "south equipment-room approach", "wing": "shared", "speed_cap": 0.25},
    "south_room_approach": {"kind": "zone", "area": "south central room approach", "wing": "shared", "speed_cap": 0.30},
}

# x, y world coordinates; all node positions are in accessible circulation area.
WAYPOINTS = {
    "lift_lobby": [0.0, 18.0],
    "north_hall": [0.0, 12.0],
    "west_north_gate": [-4.5, 14.0],
    "east_north_gate": [4.5, 14.0],
    "west_waiting_approach": [-4.5, 8.0],
    "east_waiting_approach": [4.5, 8.0],
    "west_mid_hall": [-4.5, 3.0],
    "east_mid_hall": [4.5, 3.0],
    "nurse_station_approach": [0.0, 5.0],
    "west_south_hall": [-4.5, -8.0],
    "east_south_hall": [4.5, -8.0],
    "south_equipment_approach": [0.0, -15.0],
    "south_room_approach": [0.0, -28.0],
}

EDGE_PAIRS = (
    ("lift_lobby", "north_hall"),
    ("north_hall", "west_north_gate"),
    ("north_hall", "east_north_gate"),
    ("north_hall", "nurse_station_approach"),
    ("west_north_gate", "west_waiting_approach"),
    ("east_north_gate", "east_waiting_approach"),
    ("west_waiting_approach", "west_mid_hall"),
    ("east_waiting_approach", "east_mid_hall"),
    ("west_mid_hall", "nurse_station_approach"),
    ("east_mid_hall", "nurse_station_approach"),
    ("west_mid_hall", "west_south_hall"),
    ("east_mid_hall", "east_south_hall"),
    ("west_south_hall", "south_equipment_approach"),
    ("east_south_hall", "south_equipment_approach"),
    ("south_equipment_approach", "south_room_approach"),
)

GROUPS = {
    "east": ["east_north_gate", "east_waiting_approach", "east_mid_hall", "east_south_hall"],
    "west": ["west_north_gate", "west_waiting_approach", "west_mid_hall", "west_south_hall"],
    "shared": ["lift_lobby", "north_hall", "nurse_station_approach", "south_equipment_approach", "south_room_approach"],
}


def wall_polygons():
    """Read the actual first-floor collision mesh in Gazebo's (x, y) frame."""
    dae = ROOT / "models/aws_robomaker_hospital_floor_01_walls/meshes/aws_robomaker_hospital_floor_01_walls_collision.dae"
    namespace = {"c": "http://www.collada.org/2005/11/COLLADASchema"}
    root = ET.parse(dae).getroot()
    geometry = root.find('.//c:geometry[@id="ID3"]', namespace)
    positions = geometry.find('./c:mesh/c:source[@id="ID4"]/c:float_array', namespace)
    values = list(map(float, positions.text.split()))
    # DAE node transforms map source (x, y, z) centimetres to (z, x, y) metres.
    vertices = [(values[i + 2] * 0.01, values[i] * 0.01, values[i + 1] * 0.01)
                for i in range(0, len(values), 3)]
    polygons = []
    for polylist in geometry.findall('.//c:polylist', namespace):
        inputs = polylist.findall('c:input', namespace)
        stride = max(int(item.attrib['offset']) for item in inputs) + 1
        counts = list(map(int, polylist.find('c:vcount', namespace).text.split()))
        indices = list(map(int, polylist.find('c:p', namespace).text.split()))
        offset = 0
        for count in counts:
            polygon = [vertices[indices[offset + i * stride]] for i in range(count)]
            offset += count * stride
            # Discard floor/ceiling faces; only vertical collision surfaces draw walls.
            if max(point[2] for point in polygon) - min(point[2] for point in polygon) >= 1.0:
                polygons.append([(point[0], point[1]) for point in polygon])
    return polygons


def main():
    TOPOLOGY_DIR.mkdir(exist_ok=True)
    edges = [[source, destination, round(hypot(WAYPOINTS[source][0] - WAYPOINTS[destination][0],
                                               WAYPOINTS[source][1] - WAYPOINTS[destination][1]), 2)]
             for source, destination in EDGE_PAIRS]
    topology = {
        "version": "topo-v0",
        "note": ("First-floor hospital circulation graph extracted from the Fortress hospital wall collision mesh "
                 "and placed assets. Edge weights are straight-line world-frame metres; all edges are bidirectional. "
                 "Room labels are limited to observable elevator, nurse-station, seating, and equipment cues. "
                 "Groups and wing metadata identify physical circulation sections; logical PRD ward aliases are held separately "
                 "in scenario_locations.json. No physical ward bed counts or paediatric classifications are asserted."),
        "nodes": NODES,
        "edges": edges,
        "groups": GROUPS,
    }
    (TOPOLOGY_DIR / "hospital_topology.json").write_text(json.dumps(topology, indent=2) + "\n")
    (TOPOLOGY_DIR / "topology_waypoints.json").write_text(json.dumps({
        "frame": "world", "units": "m", "waypoints": WAYPOINTS}, indent=2) + "\n")

    colors = {"east": "#d62728", "west": "#2ca02c", "shared": "#1f77b4"}
    membership = {node: group for group, nodes in GROUPS.items() for node in nodes}
    figure, axis = plt.subplots(figsize=(10, 17))
    for polygon in wall_polygons():
        closed = polygon + [polygon[0]]
        axis.plot(*zip(*closed), color="#354052", linewidth=0.55, zorder=1)
    for source, destination, _ in edges:
        axis.plot([WAYPOINTS[source][0], WAYPOINTS[destination][0]],
                  [WAYPOINTS[source][1], WAYPOINTS[destination][1]],
                  color="#202020", linewidth=1.4, zorder=3)
    for node, (x, y) in WAYPOINTS.items():
        group = membership[node]
        axis.scatter(x, y, s=55, color=colors[group], edgecolor="white", linewidth=0.8, zorder=4)
        axis.annotate(node.replace("_", "\n"), (x, y), xytext=(5, 5), textcoords="offset points",
                      fontsize=6.5, zorder=5)
    for name, color in colors.items():
        axis.scatter([], [], color=color, label=name.replace("_", " "))
    axis.set(title="Hospital first-floor topology (Gazebo world frame)", xlabel="x (m)", ylabel="y (m)")
    axis.set_aspect("equal")
    axis.set_xlim(-14, 14)
    axis.set_ylim(-37, 23)
    axis.grid(alpha=0.25)
    axis.legend(loc="lower right", fontsize=8)
    figure.savefig(TOPOLOGY_DIR / "hospital_topology.png", dpi=200, bbox_inches="tight")
    print(f"Generated {len(NODES)} nodes, {len(edges)} bidirectional edges, and {len(GROUPS)} groups.")


if __name__ == "__main__":
    main()
