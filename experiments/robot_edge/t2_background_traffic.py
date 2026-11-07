#!/usr/bin/env python3
"""T2 Gazebo scenario: one deterministic pedestrian in the east corridor.

The base hospital SDF is intentionally never edited.  This program copies it
to the T2 result bundle and adds the actor only to that generated scenario.
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from experiments.cloud import lib

BASE_WORLD = ROOT / "simulation/hospital_world/hospital_six_racecars.sdf"
OCCUPANCY = ROOT / "simulation/hospital_world/maps/hospital_static_occupancy.json"
WALK_MESH = Path("/usr/share/gazebo-11/media/models/walk.dae")
START = (4.0, -13.0)
END = (4.0, 13.0)
SPEED_MPS = 0.6
YAW_NORTH = math.pi / 2
ACTOR_NAME = "t2_east_corridor_pedestrian"
ROBOT_EAST_CENTERLINE_X = 4.5


def actor_duration() -> float:
    return math.dist(START, END) / SPEED_MPS


def occupancy_clearance(points: list[tuple[float, float]]) -> float:
    """Return center-point clearance from static collision cells in metres."""
    document = json.loads(OCCUPANCY.read_text())
    data = base64.b64decode(document["occupied"])
    resolution = float(document["resolution"])
    origin_x, origin_y = document["origin"]
    width = int(document["width"])
    occupied = [
        (origin_x + (index % width + .5) * resolution,
         origin_y + (index // width + .5) * resolution)
        for index, value in enumerate(data) if value
    ]
    return min(math.dist(point, obstacle) for point in points for obstacle in occupied)


def pedestrian_points(step: float = .1) -> list[tuple[float, float]]:
    count = round((END[1] - START[1]) / step)
    return [(START[0], START[1] + index * (END[1] - START[1]) / count) for index in range(count + 1)]


def append_actor(world: ET.Element) -> None:
    actor = ET.SubElement(world, "actor", {"name": ACTOR_NAME})
    skin = ET.SubElement(actor, "skin")
    ET.SubElement(skin, "filename").text = WALK_MESH.as_uri()
    ET.SubElement(skin, "scale").text = "1.0"
    animation = ET.SubElement(actor, "animation", {"name": "walk"})
    ET.SubElement(animation, "filename").text = WALK_MESH.as_uri()
    ET.SubElement(animation, "interpolate_x").text = "true"
    script = ET.SubElement(actor, "script")
    ET.SubElement(script, "loop").text = "false"
    ET.SubElement(script, "delay_start").text = "0"
    ET.SubElement(script, "auto_start").text = "true"
    trajectory = ET.SubElement(script, "trajectory", {"id": "0", "type": "walk", "tension": "0"})
    for when, (x, y) in ((0.0, START), (actor_duration(), END)):
        waypoint = ET.SubElement(trajectory, "waypoint")
        ET.SubElement(waypoint, "time").text = f"{when:.6f}"
        ET.SubElement(waypoint, "pose").text = f"{x:.3f} {y:.3f} 0 0 0 {YAW_NORTH:.9f}"


def add_actor(world_path: Path) -> None:
    tree = ET.parse(BASE_WORLD)
    world = tree.getroot().find("world")
    if world is None:
        raise ValueError(f"No world in {BASE_WORLD}")
    append_actor(world)
    ET.indent(tree, space="  ")
    tree.write(world_path, encoding="utf-8", xml_declaration=True)


def preflight(world_path: Path) -> dict[str, Any]:
    if not BASE_WORLD.is_file() or not OCCUPANCY.is_file() or not WALK_MESH.is_file():
        raise FileNotFoundError("T2 base world, occupancy map, or local walk.dae asset is missing")
    points = pedestrian_points()
    static_clearance = occupancy_clearance(points)
    robot_clearance = abs(ROBOT_EAST_CENTERLINE_X - START[0])
    if static_clearance < .4:
        raise ValueError(f"Pedestrian path only has {static_clearance:.2f} m static clearance")
    if robot_clearance < .5:
        raise ValueError(f"Pedestrian lane is only {robot_clearance:.2f} m from the robot centerline")
    # `ign sdf` has no Gazebo resource resolver and therefore cannot validate
    # this hospital's pre-existing model:// includes. Validate the new actor in
    # isolation; the assembled world is exercised by the headless smoke run.
    actor_document = world_path.with_name("t2_pedestrian_actor_validation.sdf")
    root = ET.Element("sdf", {"version": "1.8"})
    actor_world = ET.SubElement(root, "world", {"name": "t2_actor_validation"})
    append_actor(actor_world)
    ET.ElementTree(root).write(actor_document, encoding="utf-8", xml_declaration=True)
    checked = subprocess.run(["ign", "sdf", "-k", str(actor_document)], text=True, capture_output=True)
    if checked.returncode:
        raise RuntimeError(f"SDF validation failed:\n{checked.stderr or checked.stdout}")
    return {
        "actor": ACTOR_NAME,
        "asset": str(WALK_MESH),
        "start_xy": list(START),
        "end_xy": list(END),
        "yaw_rad": YAW_NORTH,
        "speed_mps": SPEED_MPS,
        "duration_s": actor_duration(),
        "minimum_static_clearance_m": static_clearance,
        "minimum_robot_centerline_clearance_m": robot_clearance,
        "sdf_valid": True,
    }


def preflight_world_for_test() -> dict[str, Any]:
    """Build an ephemeral T2 overlay for the regression test."""
    with tempfile.TemporaryDirectory() as temporary:
        world_path = Path(temporary) / "t2_background_traffic.sdf"
        add_actor(world_path)
        return preflight(world_path)


def run(seed: int, out: str, launch: bool) -> int:
    expected = ROOT / "results/final/T2"
    if Path(out).resolve() != expected.resolve():
        raise ValueError(f"--out must be {expected.relative_to(ROOT)} to use the shared ResultDir convention")
    rd = lib.ResultDir("T2", "Urgent delivery under background traffic", None,
                       ["--seed", str(seed), "--out", out] + (["--launch"] if launch else []), prd="T2")
    rd.reset_raw()
    rd.env.update({"source": "measured", "model": "Gazebo Fortress actor preflight", "seed": seed})
    world_path = rd.dir / "raw" / "t2_background_traffic.sdf"
    add_actor(world_path)
    row = preflight(world_path)
    row.update({"seed": seed, "world": str(world_path.relative_to(ROOT)), "mission_outcome": "NOT_RUN"})
    rd.raw("scenario", row)
    rd.summary([row])
    rd.finish(
        claim="A single local animated pedestrian follows a deterministic south-to-north east-corridor lane for T2.",
        pass_bar="Actor SDF is valid; route is free of static obstacles and stays at least 0.50 m from the robot east-corridor centerline.",
        verdict="PASS",
        measured=(f"start {START}, end {END}, {SPEED_MPS:.1f} m/s for {actor_duration():.1f} s; "
                  f"static clearance {row['minimum_static_clearance_m']:.2f} m; robot-centerline clearance {robot_clearance_text(row)} m. "
                  "Smoke result: scenario generated and SDF validated; robot mission was not commanded."),
        notes=[
            "The source hospital_six_racecars.sdf remains unchanged; the actor exists only in raw/t2_background_traffic.sdf.",
            "The actor uses the installed local /usr/share/gazebo-11/media/models/walk.dae; no runtime download is used.",
            "This actor has no collision geometry, so it is visual/dynamic background traffic and cannot physically block the planned robot route.",
        ],
    )
    if not launch:
        return 0
    env = os.environ.copy()
    root = ROOT / "simulation/hospital_world"
    resource = f"{root}/models:{root}/models/robots:{root}/fuel_models"
    env["IGN_GAZEBO_RESOURCE_PATH"] = resource + (":" + env["IGN_GAZEBO_RESOURCE_PATH"] if env.get("IGN_GAZEBO_RESOURCE_PATH") else "")
    env["GZ_SIM_RESOURCE_PATH"] = resource + (":" + env["GZ_SIM_RESOURCE_PATH"] if env.get("GZ_SIM_RESOURCE_PATH") else "")
    env.setdefault("IGN_PARTITION", "nimbus_t2_background_traffic")
    return subprocess.call(["ign", "gazebo", "-r", "-s", "--headless-rendering", str(world_path)], env=env)


def robot_clearance_text(row: dict[str, Any]) -> str:
    return f"{row['minimum_robot_centerline_clearance_m']:.2f}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--out", default="results/final/T2")
    parser.add_argument("--launch", action="store_true", help="after preflight, start T2 Gazebo headlessly")
    args = parser.parse_args()
    return run(args.seed, args.out, args.launch)


if __name__ == "__main__":
    raise SystemExit(main())
