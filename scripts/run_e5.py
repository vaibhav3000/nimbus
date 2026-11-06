#!/usr/bin/env python3
"""Runner for Experiment E5: Edge Failures, Partition Tolerance & High Availability."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from edge.benchmarks.e5_benchmark import run_e5_benchmark

if __name__ == "__main__":
    run_e5_benchmark()
