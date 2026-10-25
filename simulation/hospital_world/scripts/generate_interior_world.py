#!/usr/bin/env python3
"""Generate a warmer interior-view SDF without changing source assets.

The source AWS model renders a full, pale ceiling. This generator replaces an
old-style included ceiling with a collision-only inline model, so the roof
remains available for physics without obscuring an overhead camera.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
CEILING_INSTANCE = "aws_robomaker_hospital_floor_01_ceiling_1"
CEILING_COLLISION_URI = (
    "model://aws_robomaker_hospital_floor_01_ceiling/meshes/"
    "aws_robomaker_hospital_floor_01_ceiling_collision.dae"
)


def collision_only_ceiling(pose: str) -> ET.Element:
    """Return an inline source ceiling that retains only collision geometry."""
    model = ET.Element("model", {"name": CEILING_INSTANCE})
    ET.SubElement(model, "pose").text = pose
    ET.SubElement(model, "static").text = "true"
    link = ET.SubElement(model, "link", {"name": "link"})
    collision = ET.SubElement(link, "collision", {"name": "collision"})
    geometry = ET.SubElement(collision, "geometry")
    mesh = ET.SubElement(geometry, "mesh")
    ET.SubElement(mesh, "uri").text = CEILING_COLLISION_URI
    return model


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "hospital.sdf")
    parser.add_argument("--output", type=Path, default=ROOT / "hospital_interior.sdf")
    args = parser.parse_args()

    tree = ET.parse(args.source)
    world = tree.getroot().find("world")
    if world is None:
        raise SystemExit(f"No <world> in {args.source}")

    for index, element in enumerate(list(world)):
        if element.tag != "include" or element.findtext("name") != CEILING_INSTANCE:
            continue
        pose = element.findtext("pose")
        if not pose:
            raise SystemExit("Ceiling include has no pose")
        world.remove(element)
        world.insert(index, collision_only_ceiling(pose))
        break
    else:
        # The default world may already have the collision-only inline model.
        # Keep this generator idempotent so the optional warmer-light GUI
        # launcher continues to work after the default view is updated.
        ceiling = world.find(f"model[@name='{CEILING_INSTANCE}']")
        if ceiling is None:
            raise SystemExit(f"Ceiling instance {CEILING_INSTANCE!r} was not found")
        if ceiling.find(".//visual") is not None:
            raise SystemExit("Existing ceiling model still has a visual")

    # The original asset targets a bright, clinical Classic-Gazebo scene.
    # Lower ambient illumination restores material contrast; a subtly warm
    # key light keeps the interior readable without editing any asset texture.
    scene = world.find("scene")
    if scene is not None:
        ambient = scene.find("ambient")
        if ambient is not None:
            ambient.text = "0.28 0.27 0.25 1"
    sun = world.find("light[@name='sun']")
    if sun is not None:
        diffuse = sun.find("diffuse")
        if diffuse is not None:
            diffuse.text = "0.86 0.82 0.74 1"
        specular = sun.find("specular")
        if specular is not None:
            specular.text = "0.12 0.12 0.12 1"

    ET.indent(tree, space="  ")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    tree.write(args.output, encoding="utf-8", xml_declaration=True)
    print(f"Wrote interior-view world: {args.output}")


if __name__ == "__main__":
    main()
