#!/usr/bin/env bash
# Validate local assets, collision declarations, and an actual Fortress load.
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$ROOT"
command -v ign >/dev/null || { echo "FAIL: Gazebo Fortress command 'ign' is unavailable." >&2; exit 127; }
if ! ign gazebo --versions 2>/dev/null | grep -q '^6\.'; then
  echo "FAIL: Gazebo Fortress / ignition-gazebo6 is required." >&2
  exit 1
fi

python3 - <<'PY'
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

root = Path.cwd()
world = root / 'hospital.sdf'
tree = ET.parse(world)
if tree.getroot().attrib.get('version') != '1.8':
    raise SystemExit('FAIL: hospital.sdf is not SDF 1.8')
model_roots = (root / 'models', root / 'fuel_models')
includes = sorted({uri.text.removeprefix('model://') for uri in tree.findall('.//include/uri')
                   if uri.text and uri.text.startswith('model://')})
missing = [name for name in includes if not any((folder / name / 'model.sdf').is_file()
                                                for folder in model_roots)]
if missing:
    raise SystemExit('FAIL: unresolved model URIs: ' + ', '.join(missing))
if '-7.57769e+08' in world.read_text():
    raise SystemExit('FAIL: corrupt source-height value remains')
without_collision = []
missing_meshes = []
for name in includes:
    sdf = next(folder / name / 'model.sdf' for folder in model_roots
               if (folder / name / 'model.sdf').is_file())
    model = ET.parse(sdf)
    if not model.findall('.//collision'):
        without_collision.append(name)
    for uri in model.findall('.//uri'):
        if not uri.text or not uri.text.startswith('model://'):
            continue
        rel = uri.text.removeprefix('model://')
        if not any((folder / rel).is_file() for folder in model_roots):
            missing_meshes.append(f'{name}: {uri.text}')
if without_collision:
    raise SystemExit('FAIL: models without collision geometry: ' + ', '.join(without_collision))
if missing_meshes:
    raise SystemExit('FAIL: unresolved mesh URIs:\n' + '\n'.join(missing_meshes))
print(f'PASS: XML parsed; {len(includes)} models resolve locally; all declare collision geometry.')
PY

export IGN_GAZEBO_RESOURCE_PATH="$ROOT/models:$ROOT/fuel_models${IGN_GAZEBO_RESOURCE_PATH:+:$IGN_GAZEBO_RESOURCE_PATH}"
export GZ_SIM_RESOURCE_PATH="$ROOT/models:$ROOT/fuel_models${GZ_SIM_RESOURCE_PATH:+:$GZ_SIM_RESOURCE_PATH}"
LOG="$(mktemp)"
trap 'rm -f "$LOG"' EXIT
if ! ign gazebo -r -s --iterations 10 -v 2 "$ROOT/hospital.sdf" >"$LOG" 2>&1; then
  cat "$LOG" >&2
  echo "FAIL: Fortress could not load and step hospital.sdf." >&2
  exit 1
fi
if grep -Eq '\[Err\]|Unable to find uri|Error Code' "$LOG"; then
  cat "$LOG" >&2
  echo "FAIL: Fortress reported a resource or SDF error." >&2
  exit 1
fi
echo "PASS: Gazebo Fortress loaded hospital.sdf and completed 10 DART physics iterations."
python3 "$ROOT/tools/check_spawn_clearance.py"
