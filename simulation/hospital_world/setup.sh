#!/usr/bin/env bash
# Compatibility provisioning entry point. Launches do not call this script.
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
python3 "$ROOT/tools/convert_hospital.py"
python3 "$ROOT/tools/vendor_fuel_models.py" \
  --world "$ROOT/hospital.sdf" \
  --bundled-models "$ROOT/models" \
  --destination "$ROOT/fuel_models"
