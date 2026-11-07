#!/usr/bin/env python3
"""T2 Gazebo scenario builder.

The base hospital SDF is intentionally never edited.  The generated T2 SDF is
currently an exact copy because the proposed pedestrian actor did not load in
the complete Fortress world.
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import os
from pathlib import Path
import shutil
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
START = (4.0, -13.0)
END = (4.0, 13.0)
SPEED_MPS = 0.6
YAW_NORTH = math.pi / 2
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


def build_t2_world(world_path: Path) -> None:
    """Create a T2 world without the actor pending full-world actor support.

    The previous actor overlay passed a standalone SDF grammar check but did
    not demonstrate that the complete Fortress world could load it.  Keep T2
    loadable by copying the known-good base world unchanged.
    """
    shutil.copy2(BASE_WORLD, world_path)


def preflight(world_path: Path) -> dict[str, Any]:
    if not BASE_WORLD.is_file() or not OCCUPANCY.is_file():
        raise FileNotFoundError("T2 base world or occupancy map is missing")
    points = pedestrian_points()
    static_clearance = occupancy_clearance(points)
    robot_clearance = abs(ROBOT_EAST_CENTERLINE_X - START[0])
    if static_clearance < .4:
        raise ValueError(f"Pedestrian path only has {static_clearance:.2f} m static clearance")
    if robot_clearance < .5:
        raise ValueError(f"Pedestrian lane is only {robot_clearance:.2f} m from the robot centerline")
    ET.parse(world_path)
    return {
        "actor": None,
        "asset": None,
        "start_xy": list(START),
        "end_xy": list(END),
        "yaw_rad": YAW_NORTH,
        "speed_mps": SPEED_MPS,
        "duration_s": actor_duration(),
        "minimum_static_clearance_m": static_clearance,
        "minimum_robot_centerline_clearance_m": robot_clearance,
        "sdf_valid": "base_world_xml",
    }


def preflight_world_for_test() -> dict[str, Any]:
    """Build an ephemeral T2 overlay for the regression test."""
    with tempfile.TemporaryDirectory() as temporary:
        world_path = Path(temporary) / "t2_background_traffic.sdf"
        build_t2_world(world_path)
        return preflight(world_path)


def run(seed: int, out: str, launch: bool) -> int:
    expected = ROOT / "results/final/T2"
    if Path(out).resolve() != expected.resolve():
        raise ValueError(f"--out must be {expected.relative_to(ROOT)} to use the shared ResultDir convention")
    rd = lib.ResultDir("T2", "Urgent delivery under background traffic", None,
                       ["--seed", str(seed), "--out", out] + (["--launch"] if launch else []), prd="T2")
    rd.reset_raw()
    rd.env.update({"source": "measured", "model": "Gazebo Fortress base-world preflight", "seed": seed})
    world_path = rd.dir / "raw" / "t2_background_traffic.sdf"
    build_t2_world(world_path)
    row = preflight(world_path)
    row.update({"seed": seed, "world": str(world_path.relative_to(ROOT)), "mission_outcome": "NOT_RUN"})
    rd.raw("scenario", row)
    rd.summary([row])
    rd.finish(
        claim="T2 currently uses the unchanged base hospital world while full-world pedestrian actor loading is investigated.",
        pass_bar="The generated T2 SDF contains no unvalidated pedestrian actor overlay.",
        verdict="NOT RUN",
        measured=(f"start {START}, end {END}, {SPEED_MPS:.1f} m/s for {actor_duration():.1f} s; "
                  f"static clearance {row['minimum_static_clearance_m']:.2f} m; robot-centerline clearance {robot_clearance_text(row)} m. "
                  "Pedestrian removed from generated SDF after the assembled actor world failed to load; robot mission was not commanded."),
        notes=[
            "The source hospital_six_racecars.sdf remains unchanged; the generated T2 SDF is now an exact copy without an actor.",
            "The earlier standalone actor grammar check did not validate complete-world loading and is no longer used as a pass condition.",
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
    return subprocess.call(["ign", "gazebo", "-r", str(world_path)], env=env)


def robot_clearance_text(row: dict[str, Any]) -> str:
    return f"{row['minimum_robot_centerline_clearance_m']:.2f}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--out", default="results/final/T2")
    parser.add_argument("--launch", action="store_true", help="after preflight, start the T2 Gazebo world normally")
    args = parser.parse_args()
    return run(args.seed, args.out, args.launch)


if __name__ == "__main__":
    raise SystemExit(main())
