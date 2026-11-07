#!/usr/bin/env python3
"""Rasterize static SDF collision geometry into a navigation occupancy map.

The map deliberately uses ``<collision>`` rather than visual meshes.  It is a
static layer: live range scans and peer robots are still added at runtime.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
MODEL_ROOTS = (ROOT / "models", ROOT / "fuel_models")


def tag(node: ET.Element) -> str:
    return node.tag.rsplit("}", 1)[-1]


def child(node: ET.Element, name: str) -> ET.Element | None:
    return next((item for item in node if tag(item) == name), None)


def pose(node: ET.Element | None) -> tuple[float, float, float]:
    value = child(node, "pose") if node is not None else None
    if value is None or not value.text:
        return 0.0, 0.0, 0.0
    values = [float(x) for x in value.text.split()]
    return values[0], values[1], values[5] if len(values) >= 6 else 0.0


def compose(left: tuple[float, float, float], right: tuple[float, float, float]) -> tuple[float, float, float]:
    x, y, yaw = left
    rx, ry, ryaw = right
    return x + math.cos(yaw) * rx - math.sin(yaw) * ry, y + math.sin(yaw) * rx + math.cos(yaw) * ry, yaw + ryaw


class Raster:
    def __init__(self, resolution: float, bounds: tuple[float, float, float, float]):
        self.resolution = resolution
        self.origin_x, self.origin_y, max_x, max_y = bounds
        self.width = math.ceil((max_x - self.origin_x) / resolution)
        self.height = math.ceil((max_y - self.origin_y) / resolution)
        self.data = bytearray(self.width * self.height)

    def mark(self, x: float, y: float) -> None:
        col = int(math.floor((x - self.origin_x) / self.resolution))
        row = int(math.floor((y - self.origin_y) / self.resolution))
        if 0 <= col < self.width and 0 <= row < self.height:
            self.data[row * self.width + col] = 1

    def polygon(self, points: list[tuple[float, float]]) -> None:
        if len(points) < 3:
            return
        min_x, max_x = min(x for x, _ in points), max(x for x, _ in points)
        min_y, max_y = min(y for _, y in points), max(y for _, y in points)
        c0 = max(0, int(math.floor((min_x - self.origin_x) / self.resolution)))
        c1 = min(self.width - 1, int(math.floor((max_x - self.origin_x) / self.resolution)))
        r0 = max(0, int(math.floor((min_y - self.origin_y) / self.resolution)))
        r1 = min(self.height - 1, int(math.floor((max_y - self.origin_y) / self.resolution)))
        for row in range(r0, r1 + 1):
            y = self.origin_y + (row + .5) * self.resolution
            for col in range(c0, c1 + 1):
                x = self.origin_x + (col + .5) * self.resolution
                inside = False
                previous = len(points) - 1
                for current, (px, py) in enumerate(points):
                    qx, qy = points[previous]
                    if (py > y) != (qy > y) and x < (qx - px) * (y - py) / (qy - py) + px:
                        inside = not inside
                    previous = current
                if inside:
                    self.data[row * self.width + col] = 1

    def line(self, start: tuple[float, float], end: tuple[float, float], width: float = .12) -> None:
        distance = math.dist(start, end)
        steps = max(1, math.ceil(distance / self.resolution))
        # Collision meshes contain many tessellated faces.  One cell is
        # sufficient at the map resolution; inflation for the robot happens
        # later in the navigation costmap.
        radius = max(0, int(width / (2 * self.resolution)))
        data, resolution, origin_x, origin_y = self.data, self.resolution, self.origin_x, self.origin_y
        for step in range(steps + 1):
            fraction = step / steps
            x = start[0] + (end[0] - start[0]) * fraction
            y = start[1] + (end[1] - start[1]) * fraction
            for dx in range(-radius, radius + 1):
                for dy in range(-radius, radius + 1):
                    if dx * dx + dy * dy <= radius * radius:
                        col = int(math.floor((x + dx * resolution - origin_x) / resolution))
                        row = int(math.floor((y + dy * resolution - origin_y) / resolution))
                        if 0 <= col < self.width and 0 <= row < self.height:
                            data[row * self.width + col] = 1

    def circle(self, centre: tuple[float, float], radius: float) -> None:
        segments = 20
        self.polygon([(centre[0] + radius * math.cos(2 * math.pi * i / segments), centre[1] + radius * math.sin(2 * math.pi * i / segments)) for i in range(segments)])


def transform(point: tuple[float, float], frame: tuple[float, float, float]) -> tuple[float, float]:
    x, y, yaw = frame
    return x + math.cos(yaw) * point[0] - math.sin(yaw) * point[1], y + math.sin(yaw) * point[0] + math.cos(yaw) * point[1]


def model_path(uri: str) -> Path | None:
    if not uri.startswith("model://"):
        return None
    name = uri.removeprefix("model://").split("/", 1)[0]
    return next((root / name / "model.sdf" for root in MODEL_ROOTS if (root / name / "model.sdf").is_file()), None)


def mesh_path(uri: str, current_model: Path) -> Path | None:
    if uri.startswith("model://"):
        relative = uri.removeprefix("model://")
        model, _, rest = relative.partition("/")
        return next((root / model / rest for root in MODEL_ROOTS if (root / model / rest).is_file()), None)
    candidate = current_model.parent / uri
    return candidate if candidate.is_file() else None


def collada_triangles(path: Path) -> list[list[tuple[float, float, float]]]:
    """Read position triangles from the subset of Collada used by Fuel models."""
    root = ET.parse(path).getroot()
    asset = next((node for node in root if tag(node) == "asset"), None)
    unit = child(asset, "unit") if asset is not None else None
    metre = float(unit.attrib.get("meter", "1")) if unit is not None else 1.0
    up_axis = (child(asset, "up_axis").text or "Z_UP").strip() if asset is not None and child(asset, "up_axis") is not None else "Z_UP"
    arrays = {node.attrib.get("id"): [float(v) for v in (node.text or "").split()] for node in root.iter() if tag(node) == "float_array"}
    sources: dict[str, tuple[list[float], int]] = {}
    for node in root.iter():
        if tag(node) != "source":
            continue
        array = child(node, "float_array")
        accessor = next((item for item in node.iter() if tag(item) == "accessor"), None)
        if array is not None and accessor is not None:
            sources[node.attrib["id"]] = (arrays.get(array.attrib.get("id"), []), int(accessor.attrib.get("stride", "3")))
    vertices: dict[str, str] = {}
    for node in root.iter():
        if tag(node) == "vertices":
            item = next((v for v in node if tag(v) == "input" and v.attrib.get("semantic") == "POSITION"), None)
            if item is not None:
                vertices[node.attrib["id"]] = item.attrib["source"].lstrip("#")
    result: list[list[tuple[float, float, float]]] = []
    for primitive in root.iter():
        if tag(primitive) not in {"triangles", "polylist", "polygons"}:
            continue
        inputs = [item for item in primitive if tag(item) == "input"]
        if not inputs:
            continue
        stride = max(int(item.attrib.get("offset", "0")) for item in inputs) + 1
        pos_input = next((item for item in inputs if item.attrib.get("semantic") in {"VERTEX", "POSITION"}), None)
        if pos_input is None:
            continue
        source_id = pos_input.attrib["source"].lstrip("#")
        source_id = vertices.get(source_id, source_id)
        values, position_stride = sources.get(source_id, ([], 3))
        if not values:
            continue
        offset = int(pos_input.attrib.get("offset", "0"))
        groups: list[list[int]] = []
        if tag(primitive) == "polylist":
            counts = [int(v) for v in (child(primitive, "vcount").text or "").split()]
            indices = [int(v) for v in (child(primitive, "p").text or "").split()]
            cursor = 0
            for count in counts:
                groups.append(indices[cursor:cursor + count * stride]); cursor += count * stride
        else:
            groups = [[int(v) for v in (node.text or "").split()] for node in primitive if tag(node) == "p"]
        for group in groups:
            vertices_3d = []
            for index in range(0, len(group), stride):
                vertex = group[index + offset] * position_stride
                if vertex + 2 < len(values):
                    x, y, z = values[vertex] * metre, values[vertex + 1] * metre, values[vertex + 2] * metre
                    # This hospital's Fuel mesh is Y-up and its SDF asset
                    # transform maps source (x, y, z) to Gazebo (z, x, y).
                    # Keep this aligned with generate_topology.py, which is
                    # the existing authority for the hospital world frame.
                    vertices_3d.append((z, x, y) if up_axis == "Y_UP" else (x, y, z))
            for index in range(1, len(vertices_3d) - 1):
                result.append([vertices_3d[0], vertices_3d[index], vertices_3d[index + 1]])
    return result


def paint_geometry(raster: Raster, geometry: ET.Element, frame: tuple[float, float, float], source: Path) -> int:
    if (box := child(geometry, "box")) is not None:
        size = [float(v) for v in (child(box, "size").text or "").split()]
        x, y = size[:2]
        raster.polygon([transform(point, frame) for point in ((-x / 2, -y / 2), (x / 2, -y / 2), (x / 2, y / 2), (-x / 2, y / 2))])
        return 1
    if (cylinder := child(geometry, "cylinder")) is not None:
        radius = float((child(cylinder, "radius").text or "0"))
        raster.circle((frame[0], frame[1]), radius)
        return 1
    if (sphere := child(geometry, "sphere")) is not None:
        radius = float((child(sphere, "radius").text or "0"))
        raster.circle((frame[0], frame[1]), radius)
        return 1
    if (mesh := child(geometry, "mesh")) is not None and (uri := child(mesh, "uri")) is not None and uri.text:
        path = mesh_path(uri.text.strip(), source)
        if path is None or path.suffix.lower() != ".dae":
            return 0
        scale = [float(v) for v in ((child(mesh, "scale").text if child(mesh, "scale") is not None else "1 1 1").split())]
        triangles = collada_triangles(path)
        # Fuel collision assets can be heavily tessellated.  At 20 cm map
        # resolution, evenly sampling a mesh preserves its envelope while
        # keeping regeneration fast enough for normal developer workflows.
        step = max(1, math.ceil(len(triangles) / 800))
        count = 0
        for triangle in triangles[::step]:
            # Horizontal mesh faces are floors, shelves, or ceilings.  Their
            # projected outlines are not ground-level obstacles; only a
            # vertical face can block the robot footprint.
            if max(vertex[2] for vertex in triangle) - min(vertex[2] for vertex in triangle) < .15:
                continue
            points = [transform((vertex[0] * scale[0], vertex[1] * scale[1]), frame) for vertex in triangle]
            # Mesh edges preserve wall and furniture footprints without
            # filling large vertical mesh projections as if they were floors.
            for index in range(3):
                raster.line(points[index], points[(index + 1) % 3])
            count += 1
        return count
    return 0


def paint_model(raster: Raster, sdf_path: Path, frame: tuple[float, float, float]) -> int:
    root = ET.parse(sdf_path).getroot()
    model = next((node for node in root.iter() if tag(node) == "model"), None)
    if model is None:
        return 0
    frame = compose(frame, pose(model))
    count = 0
    for link in (node for node in model if tag(node) == "link"):
        link_frame = compose(frame, pose(link))
        for collision in (node for node in link if tag(node) == "collision"):
            geometry = child(collision, "geometry")
            if geometry is not None:
                count += paint_geometry(raster, geometry, compose(link_frame, pose(collision)), sdf_path)
    return count


def build_map(sdf_path: Path, raster: Raster) -> int:
    root = ET.parse(sdf_path).getroot()
    world = next((node for node in root.iter() if tag(node) == "world"), None)
    if world is None:
        raise ValueError(f"No world in {sdf_path}")
    count = 0
    for include in (node for node in world if tag(node) == "include"):
        uri = child(include, "uri")
        if uri is not None and uri.text and (path := model_path(uri.text.strip())) is not None:
            count += paint_model(raster, path, pose(include))
    return count


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sdf", type=Path, default=ROOT / "hospital.sdf")
    parser.add_argument("--output", type=Path, default=ROOT / "maps" / "hospital_static_occupancy.json")
    parser.add_argument("--resolution", type=float, default=.20)
    parser.add_argument("--bounds", type=float, nargs=4, default=(-16., 16., -30., 22.), metavar=("MIN_X", "MAX_X", "MIN_Y", "MAX_Y"))
    args = parser.parse_args()
    min_x, max_x, min_y, max_y = args.bounds
    raster = Raster(args.resolution, (min_x, min_y, max_x, max_y))
    geometries = build_map(args.sdf, raster)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "format": "nimbus-static-occupancy-v1", "resolution": raster.resolution,
        "origin": [raster.origin_x, raster.origin_y], "width": raster.width, "height": raster.height,
        "occupied": base64.b64encode(bytes(raster.data)).decode("ascii"),
        "source_sdf": str(args.sdf), "source_sha256": hashlib.sha256(args.sdf.read_bytes()).hexdigest(),
        "collision_geometries": geometries,
    }
    args.output.write_text(json.dumps(payload, separators=(",", ":")) + "\n")
    print(f"wrote {args.output}: {raster.width}x{raster.height} at {raster.resolution:.2f} m ({sum(raster.data)} occupied cells from {geometries} collision geometries)")


if __name__ == "__main__":
    main()
