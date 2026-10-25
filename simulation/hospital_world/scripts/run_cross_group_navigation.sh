#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source /opt/ros/humble/setup.bash
export PYTHONPATH="${ROOT_DIR}${PYTHONPATH:+:${PYTHONPATH}}"
export IGN_PARTITION="${IGN_PARTITION:-custom_hospital_world}"

exec python3 "${ROOT_DIR}/scripts/run_cross_group_navigation.py"
