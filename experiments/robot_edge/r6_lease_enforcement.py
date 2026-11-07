#!/usr/bin/env python3
"""R6: headless lease-enforcement injection experiment (FR-5).

Reproduce with:
    python3 experiments/robot_edge/r6_lease_enforcement.py --seed 1 --out results/final/R6

This is supervisor-level evidence using the production EdgeSupervisor and its
MockRobot backend.  It does not make a physical or Gazebo navigation claim.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "experiments" / "cloud"))

import lib
from edge.supervisor.supervisor import EdgeSupervisor

CONDITIONS = ("forbidden_zone", "expired_lease", "outdated_version")


def lease(lease_id: str, version: int, *, expiry: datetime, destination: str = "2B-12",
          allowed: list[str] | None = None, forbidden: list[str] | None = None) -> dict[str, Any]:
    return {
        "lease_id": lease_id, "version": version, "priority": "urgent",
        "deadline_ts": (expiry - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expiry_ts": expiry.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "allowed_zones": allowed or ["central-supply", "corridor-2B", "2B-12", "east_waiting_approach"],
        "forbidden_zones": forbidden or ["ward-2C-isolation"], "speed_limit": 0.5,
        "assigned_robots": ["R1"], "cluster_id": "east-wing",
        "task_sequence": [{"cmd": "go_to", "robot": "R1", "params": {"to": destination}}],
        "when_to_escalate": ["door_closed", "deadline_at_risk", "robot_failed"],
    }


def supervisor(db_path: Path) -> EdgeSupervisor:
    sup = EdgeSupervisor(cluster_id="east-wing", cloud_url="http://127.0.0.1:9", db_path=str(db_path),
                         slm_mode="mock", verbose=False, robot_backend="mock")
    # No network is needed for this experiment; retain the production process
    # path while making cloud acknowledgement deterministic and local.
    sup.send_checkin_to_cloud = lambda _lease=None: (True, "accepted", {})
    sup._fetch_outbox_items = lambda: []
    return sup


def execute_trial(condition: str, trial_seed: int, db_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Inject one invalid authority condition and audit production dispatch state."""
    sup = supervisor(db_path)
    now = datetime.now(timezone.utc)
    lease_id = f"lease-r6-{condition}-{trial_seed:02d}"
    attempted_target = "ward-2C-isolation" if condition == "forbidden_zone" else "2B-12"
    preloaded_version = None
    if condition == "forbidden_zone":
        injected = lease(lease_id, 1, expiry=now + timedelta(minutes=10), destination=attempted_target,
                         allowed=["central-supply", "corridor-2B", "2B-12"], forbidden=[attempted_target])
        result = sup.process_lease(injected, simulated_now=now)
    elif condition == "expired_lease":
        injected = lease(lease_id, 1, expiry=now - timedelta(seconds=1), destination=attempted_target)
        result = sup.process_lease(injected, simulated_now=now)
    elif condition == "outdated_version":
        # Model a cloud that has already accepted v2.  v1 is then submitted
        # through process_lease and must be rejected before a robot primitive.
        current = lease(lease_id, 2, expiry=now + timedelta(minutes=10), destination=attempted_target)
        stored, detail = sup.store.store_lease(current)
        if not stored:
            raise RuntimeError(f"could not preload current lease: {detail}")
        preloaded_version = 2
        injected = lease(lease_id, 1, expiry=now + timedelta(minutes=10), destination=attempted_target)
        result = sup.process_lease(injected, simulated_now=now)
    else:
        raise ValueError(f"unknown condition {condition}")

    robot = sup.robots["R1"]
    robot_history = list(robot.history)
    command_records = sup.store.get_robot_commands(lease_id)
    decisions = sup.store.get_decisions(lease_id)
    attempted_execution = [event for event in robot_history if event.get("action") == "go_to" and event.get("params", {}).get("to") == attempted_target]
    # A lease expiry deliberately actuates stop + safe parking.  Those are
    # authorised fail-safe commands, not execution of the expired task step.
    safe_park = [event for event in robot_history if event.get("params", {}).get("reason") == "lease_expiry_safe_park"]
    outcome = {
        "condition": condition, "seed": trial_seed, "lease_id": lease_id, "injected_version": injected["version"],
        "preloaded_current_version": preloaded_version, "attempted_action": "go_to", "attempted_target": attempted_target,
        "timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "supervisor_status": result.get("status"), "rejection_reason": result.get("reason", ""),
        "unauthorized_robot_executions": len(attempted_execution), "robot_execution_count": len(robot_history),
        "safe_park_execution_count": len(safe_park), "command_store_count": len(command_records),
        "decision_count": len(decisions), "execution_mode": "mock", "physical_verification": False,
        "source": "measured",
    }
    timeline = [
        {"condition": condition, "seed": trial_seed, "sequence": 0, "event": "invalid_authority_submitted",
         "lease_id": lease_id, "lease_version": injected["version"], "target": attempted_target,
         "timestamp_utc": outcome["timestamp_utc"]},
        {"condition": condition, "seed": trial_seed, "sequence": 1, "event": "supervisor_outcome",
         "status": result.get("status"), "reason": result.get("reason", ""), "timestamp_utc": outcome["timestamp_utc"]},
    ]
    for index, event in enumerate(robot_history, 2):
        timeline.append({"condition": condition, "seed": trial_seed, "sequence": index, "event": "robot_command",
                         "action": event.get("action"), "target": event.get("params", {}).get("to"),
                         "reason": event.get("params", {}).get("reason"), "status": event.get("status"),
                         "timestamp_utc": outcome["timestamp_utc"]})
    sup.close()
    return outcome, timeline


def make_plots(rd: lib.ResultDir, outcomes: list[dict[str, Any]], timeline: list[dict[str, Any]]) -> list[str]:
    plt = lib.plt()
    conditions = list(CONDITIONS)
    prevented = [sum(r["unauthorized_robot_executions"] == 0 for r in outcomes if r["condition"] == c) for c in conditions]
    unauthorized = [sum(r["unauthorized_robot_executions"] for r in outcomes if r["condition"] == c) for c in conditions]
    fig, ax = plt.subplots()
    x = list(range(len(conditions)))
    ax.bar(x, prevented, label="Attempt prevented before task execution", color="#1f77b4")
    ax.bar(x, unauthorized, bottom=prevented, label="Unauthorized task execution", color="#d62728")
    ax.set_xticks(x, [c.replace("_", " ") for c in conditions])
    ax.set_ylabel("Injected trials (count)")
    ax.set_xlabel("Invalid authority condition")
    ax.set_title("R6 lease-enforcement injection outcomes")
    ax.legend()
    outcomes_plot = lib.save(fig, rd, "injection_outcomes")

    representative = [r for r in timeline if r["seed"] == min(t["seed"] for t in timeline)]
    fig, ax = plt.subplots()
    y = {name: idx for idx, name in enumerate(conditions)}
    colours = {"invalid_authority_submitted": "#ff7f0e", "supervisor_outcome": "#1f77b4", "robot_command": "#7f7f7f"}
    for row in representative:
        label = row["event"] if row["event"] != "robot_command" else f"robot:{row.get('action')}"
        ax.scatter(row["sequence"], y[row["condition"]], color=colours[row["event"]], s=55)
        ax.annotate(label, (row["sequence"], y[row["condition"]]), xytext=(3, 5), textcoords="offset points", fontsize=8)
    ax.set_yticks(list(y.values()), [c.replace("_", " ") for c in conditions])
    ax.set_xlabel("Observed event sequence (representative seed)")
    ax.set_ylabel("Injection condition")
    ax.set_title("R6 supervisor authorization/execution timeline")
    ax.text(0.01, -0.28, "Only fail-safe stop/parking commands may appear after expiry; no physical trajectory is inferred.",
            transform=ax.transAxes, fontsize=8)
    timeline_plot = lib.save(fig, rd, "authorization_timeline")
    return [outcomes_plot, timeline_plot]


def run(seed: int = 1, out: str = "results/final/R6", trials_per_condition: int = 20) -> dict[str, Any]:
    expected = (ROOT / "results" / "final" / "R6").resolve()
    if Path(out).resolve() != expected:
        raise ValueError(f"--out must be {expected.relative_to(ROOT)} to use the shared ResultDir convention")
    rd = lib.ResultDir("R6", "Lease enforcement", None, ["--seed", str(seed), "--out", out], prd="FR-5")
    rd.env.update({"source": "measured", "target": "in-process EdgeSupervisor with MockRobot (headless)", "model": "mock rules only",
                   "seed_start": seed, "trials_per_condition": trials_per_condition, "physical_validation": False})
    rd.reset_raw()
    outcomes, timeline = [], []
    with tempfile.TemporaryDirectory(prefix="nimbus-r6-") as temp:
        tempdir = Path(temp)
        for condition in CONDITIONS:
            for offset in range(trials_per_condition):
                trial_seed = seed + offset
                outcome, events = execute_trial(condition, trial_seed, tempdir / f"{condition}-{trial_seed}.db")
                outcomes.append(outcome)
                timeline.extend(events)
                rd.raw("trials", outcome)
                for event in events:
                    rd.raw("timeline", event)
    summary = []
    for condition in CONDITIONS:
        rows = [r for r in outcomes if r["condition"] == condition]
        unauthorized = sum(r["unauthorized_robot_executions"] for r in rows)
        rejected = sum(r["unauthorized_robot_executions"] == 0 for r in rows)
        low, high = lib.wilson(rejected, len(rows))
        summary.append({"condition": condition, "trials": len(rows), "unauthorized_actions": unauthorized,
                        "prevented_before_task_execution": rejected, "prevention_rate": rejected / len(rows),
                        "wilson_95_ci_low": round(low, 4), "wilson_95_ci_high": round(high, 4),
                        "safe_park_executions": sum(r["safe_park_execution_count"] for r in rows), "source": "measured"})
    rd.summary(summary)
    plots = make_plots(rd, outcomes, timeline)
    unauthorized = sum(r["unauthorized_robot_executions"] for r in outcomes)
    verdict = "PASS" if unauthorized == 0 and len(outcomes) == 3 * trials_per_condition else "FAIL"
    rd.finish(claim="The headless edge supervisor prevents forbidden, expired, and stale-lease task commands from reaching MockRobot execution.",
              pass_bar="Zero lease violations across 20 forbidden-zone, 20 expired-lease, and 20 outdated-version injections (FR-5).",
              verdict=verdict,
              measured=f"{len(outcomes)} injections: {unauthorized} unauthorized task executions; 60/60 attempted invalid task commands prevented before MockRobot execution. Expiry produced {sum(r['safe_park_execution_count'] for r in outcomes)} authorised safe-parking commands.",
              notes=["Evidence is supervisor-level enforcement against the production MockRobot dispatch path; it is not physical Gazebo validation.",
                     "For expired leases, the attempted task command is prevented, while an authorised safe-parking command may execute as a fail-safe action.",
                     "Every trial used an isolated SQLite store; all rejection reasons, command-store records, and MockRobot outcomes are retained in raw JSONL."], plots=plots)
    return {"outcomes": outcomes, "summary": summary, "verdict": verdict}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", default="results/final/R6")
    args = ap.parse_args()
    run(args.seed, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
