"""NFR-6 / E6: re-run an experiment from its seed and diff the two decision logs byte for byte.

    python scripts/replay_check.py --seed 7                       # runs a seeded scenario twice in separate processes, compares
    python scripts/replay_check.py --dump a.json --seed 7         # write one run's decision log
    python scripts/replay_check.py --compare a.json b.json        # diff two dumps (e.g. GET /tasks/{id}/timeline from two runs)

The scenario is seeded (which requests, which incidents), runs on a fixed clock with the offline model (or a cassette: set NIMBUS_LLM=bedrock
NIMBUS_CASSETTE=... NIMBUS_CASSETTE_MODE=replay), and logs every decision row. Two runs are started with different PYTHONHASHSEED values so
that any dependence on dictionary or set order would show up as a difference. Exit code 1 if the logs differ.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import random
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("NIMBUS_PACK", "hospital_sim")

REQUESTS = ["Deliver a wheelchair to ward 3, routine.", "Need a monitor at 2B, urgent.", "Deliver a blanket to 2C, routine.", "Move a monitor from 2B to 2C, routine",
            "Count the idle infusion pumps", "a pump to 2B", "Deliver an iv pole to ward 3 within 20 minutes."]


def scenario(seed: int) -> list[dict]:
    from cloud.clock import FixedClock
    from cloud.orchestrator import Orchestrator
    rng = random.Random(seed)
    clock = FixedClock()
    with tempfile.TemporaryDirectory() as d:
        os.environ["NIMBUS_MODE"] = "C1"
        orch = Orchestrator.from_env(Path(d), clock=clock)
        for cid in orch.fleet.clusters:
            orch.fleet.heartbeat(cid, {})
        rows, tasks = [], {}

        def keep(out):
            rows.extend({"who": r["who"], "inputs": r["inputs"], "output": r["output"], "lease_version": r["lease_version"]} for r in out.rows)

        for n in range(1, 9):
            text = rng.choice(REQUESTS)
            t = {"task_id": f"task-{n:06d}", "request_text": text, "status": "received"}
            out = orch.handle_request(t, text)
            keep(out)
            if out.kind == "clarify":
                out = orch.handle_request(t, text, question=out.question, answer=rng.choice(["Yes, a paediatric pump please.", "No, standard is fine."]))
                keep(out)
            t.update(status=out.kind, spec=out.spec, lease=out.lease, legs=out.legs, leg_index=out.leg_index or 0, reservation=out.reservation)
            tasks[t["task_id"]] = t
            clock.advance(rng.choice([5, 30, 90]))
            if out.kind == "leased" and rng.random() < 0.5:
                react = orch.react.on_event(t, {"what_happened": "corridor blocked", "where": rng.choice(["east_mid_hall", "west_mid_hall", "east_north_gate"]),
                                                "robots_saw": ["cart"]})
                rows.extend({"who": r["who"], "inputs": r["inputs"], "output": r["output"], "lease_version": r["lease_version"]} for r in react.rows)
                if react.outcome is not None:
                    keep(react.outcome)
        for tid, t in tasks.items():
            if t["status"] == "leased" and rng.random() < 0.6:
                orch.finish(t); t["status"] = "completed"
        for _, o in orch.drain(tasks.get):
            keep(o)
        return rows


def digest(rows: list[dict]) -> str:
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


def compare(a: list[dict], b: list[dict]) -> list[str]:
    strip = lambda rows: [json.dumps({k: v for k, v in r.items() if k != "timestamp"}, sort_keys=True, indent=1) for r in rows]
    return list(difflib.unified_diff(strip(a), strip(b), "run A", "run B", lineterm="", n=2))


def run_in_subprocess(seed: int, hashseed: str) -> list[dict]:
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name
    env = {**os.environ, "PYTHONHASHSEED": hashseed}
    subprocess.run([sys.executable, __file__, "--seed", str(seed), "--dump", path], check=True, env=env, capture_output=True)
    return json.loads(Path(path).read_text())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--dump")
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"))
    a = ap.parse_args(argv)
    if a.compare:
        diff = compare(*(json.loads(Path(p).read_text()) for p in a.compare))
    elif a.dump:
        rows = scenario(a.seed)
        Path(a.dump).write_text(json.dumps(rows, sort_keys=True))
        print(f"{len(rows)} decision rows, sha256 {digest(rows)[:16]}")
        return 0
    else:
        one, two = run_in_subprocess(a.seed, "1"), run_in_subprocess(a.seed, "2")
        print(f"run A: {len(one)} rows, {digest(one)[:16]}   run B: {len(two)} rows, {digest(two)[:16]}")
        diff = compare(one, two)
    print("IDENTICAL" if not diff else "DIFFERENT\n" + "\n".join(diff[:60]))
    return 0 if not diff else 1


if __name__ == "__main__":
    sys.exit(main())
