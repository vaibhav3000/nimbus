#!/usr/bin/env python3
"""T5 -- Gate closed: cross-wing hand-over, on the REAL deployed cloud (PRD SC-2/FR-16).

Scenario (your design, real map, no pack change):
  Deliver the paediatric infusion pump to ward 2B-12.  The pickup is the shared store
  (south_equipment_approach), reachable only via the two south corners; the east
  wing's corner (east_south_hall_corner) is reported CLOSED by the edge.  So the east
  wing cannot reach the pickup, and the job must cross wings: the RIGHT (west) wing
  collects at the store and the pump reaches 2B via a hand-off between the wings.
  (On the deployed cloud this shows up as a two-leg hand-over: west carries the pump
  from the store to the shared hand-off zone, east carries it from there to 2B.)

Cloud (per the project rule -- C1/C2 ALWAYS use the deployed endpoints):
  * C1 -> https://qrgdidp9te...  (real cloud, real Bedrock)
  * C2 -> https://xkczmnuqla...  (real cloud, its own endpoint)
  * C3 -> a local cloud (classical, no cloud LLM) on the real hospital_sim6 pack.

Edge: scripts/edge_sim.py -- the legitimate HTTP edge client -- one per wing, kinematic
(--pace).  The east client reports the closed corner with `--close east_south_hall_corner`
(the real edge closure contract); the cloud reacts and arranges the cross-wing hand-over.
This is a REAL run against the deployed cloud (creates real tasks; shows in the operator GUI).

We MEASURE, per config: whether a cross-wing hand-over happened, the leg sequence of
wings, who decided it (LLM vs rules), and completion.  Ideal order C1 > C2 >> C3.

Usage (normally via T5.sh):
    python3 scripts/run_t5.py --mode C1 --seed 1
    python3 scripts/run_t5.py --plot-only
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "results" / "final" / "T5"
RAW = OUT_DIR / "raw"

ENDPOINTS = {
    "C1": os.environ.get("CLOUD_C1", "https://qrgdidp9te.execute-api.ap-south-1.amazonaws.com"),
    "C2": os.environ.get("CLOUD_C2", "https://xkczmnuqla.execute-api.ap-south-1.amazonaws.com"),
    # C3 is local (classical, no cloud LLM); launched by this script.
}
REQUEST_TEXT = "Need a pediatric infusion pump at 2B-12, urgent."
CLOSED_CORNER = os.environ.get("T5_CLOSED_CORNER", "east_south_hall_corner")   # east's only link to the shared store
URGENT_DEADLINE_S = 12 * 60
PACE = float(os.environ.get("T5_PACE", "20"))
TRIAL_TIMEOUT_S = 150.0


def _rstrip(u: str) -> str:
    return u.rstrip("/")


def _get(base: str, p: str, timeout: float = 20.0):
    try:
        with urllib.request.urlopen(base + p, timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}")
    except Exception as e:
        return {"error": str(e)}


def _post(base: str, p: str, body: dict, timeout: float = 25.0):
    req = urllib.request.Request(base + p, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"error": str(e)}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_up(base: str, timeout: float = 30.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if "error" not in _get(base, "/fleet", timeout=4.0):
            return True
        time.sleep(0.5)
    return False


def _edge(base: str, cluster: str, data_dir: Path, close: str | None) -> subprocess.Popen:
    cmd = [sys.executable, "-u", "scripts/edge_sim.py", "--api", base, "--cluster", cluster,
           "--complete", "--pace", str(PACE), "--interval", "1"]
    if close:
        cmd += ["--close", close]
    log = (data_dir / f"edge_{cluster}.log").open("w")
    return subprocess.Popen(cmd, cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT)


def run_trial(mode: str, seed: int) -> dict:
    RAW.mkdir(parents=True, exist_ok=True)
    data_dir = Path(tempfile.mkdtemp(prefix=f"t5-{mode}-"))
    trial = {"mode": mode, "seed": seed, "closed_corner": CLOSED_CORNER,
             "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    cloud_proc = None
    procs: list[subprocess.Popen] = []
    try:
        # ---- choose the cloud: C1/C2 deployed, C3 local ----
        if mode in ENDPOINTS:
            base = _rstrip(ENDPOINTS[mode])
            trial["cloud_url"] = base
            trial["cloud_kind"] = "deployed"
        else:  # C3 local
            port = _free_port()
            base = f"http://127.0.0.1:{port}"
            env = dict(os.environ)
            env.update(NIMBUS_MODE="C3", NIMBUS_PACK=os.environ.get("NIMBUS_PACK", "hospital_sim6"),
                       NIMBUS_LLM="fake", NIMBUS_BACKEND="local", PORT=str(port), DATA_DIR=str(data_dir),
                       NIMBUS_TICK_S="1")
            log = (data_dir / "cloud.log").open("w")
            cloud_proc = subprocess.Popen([sys.executable, "-u", "server.py"], cwd=str(ROOT), env=env,
                                          stdout=log, stderr=subprocess.STDOUT)
            trial["cloud_url"] = base
            trial["cloud_kind"] = "local"
            if not _wait_up(base):
                trial.update(success=False, failure_reason="local C3 cloud did not start")
                return trial
        try:
            trial["cloud"] = {k: _get(base, "/fleet").get(k) for k in ("mode", "llm", "pack")}
        except Exception:
            trial["cloud"] = {}

        _post(base, "/reset-memory", {})            # fresh habits/closures/restrictions for a clean run

        # ---- real edge clients (kinematic); east reports the closed corner ----
        procs.append(_edge(base, "east-wing", data_dir, close=CLOSED_CORNER))
        procs.append(_edge(base, "west-wing", data_dir, close=None))
        time.sleep(4)   # let both wings heartbeat so the cloud sees healthy clusters

        # ---- the mission ----
        t_submit = time.time()
        st, body = _post(base, "/requests", {"text": REQUEST_TEXT, "message_id": f"t5-{mode}-{seed}-{int(t_submit)}"})
        task_id = body.get("task_id")
        if body.get("status") == "needs_clarification" and task_id:
            _post(base, f"/requests/{task_id}/answer",
                  {"text": "Yes, a paediatric pump.", "message_id": f"t5-{mode}-{seed}-ans-{int(t_submit)}"})
        if not task_id:
            trial.update(success=False, failure_reason=f"no task_id (status={body.get('status')}, reason={body.get('reason')})")
            return trial
        trial["task_id"] = task_id

        # ---- drive to completion; record the wings that held the lease over time ----
        seen_clusters: list[str] = []
        versions: list[int] = []
        t_leased = t_done = None
        final_status = None
        end = t_submit + TRIAL_TIMEOUT_S
        while time.time() < end:
            t = _get(base, f"/tasks/{task_id}")
            ls = t.get("lease") or {}
            c, v = ls.get("cluster_id"), ls.get("version")
            if c and (not seen_clusters or seen_clusters[-1] != c):
                seen_clusters.append(c)
            if v and v not in versions:
                versions.append(v)
            if t_leased is None and ls:
                t_leased = time.time()
            final_status = t.get("status")
            if final_status in ("completed", "rejected", "cancelled", "failed"):
                t_done = time.time()
                break
            time.sleep(1.0)

        # ---- decision trace ----
        tl = _get(base, f"/tasks/{task_id}/timeline")
        rows = tl.get("timeline", []) if isinstance(tl, dict) else []
        blob = json.dumps(rows).lower()
        react = [r for r in rows if (r.get("output") or {}).get("decision") == "react"]
        handover_planned = ("plan_handover" in blob) or ("advance_leg" in blob) or ("reassign" in blob) \
            or ("hand-over" in blob) or ("handoff" in blob)
        wings = sorted(set(w for w in seen_clusters if w))
        cross_wing = len(wings) >= 2 or handover_planned
        decided_by = None
        for r in react:
            decided_by = (r.get("output") or {}).get("source") or decided_by
        trace = _get(base, f"/tasks/{task_id}/trace")
        events = trace.get("events", []) if isinstance(trace, dict) else []
        for e in events:
            with (RAW / "decision_log.jsonl").open("a") as f:
                f.write(json.dumps({"mode": mode, "seed": seed, "task_id": task_id, **e}, default=str) + "\n")
        llm_calls = sum(1 for e in events if e.get("kind") == "llm")
        completed = final_status == "completed"

        trial.update(
            success=completed and cross_wing,
            completed=completed,
            final_status=final_status,
            cross_wing_handover=cross_wing,
            wing_sequence=seen_clusters,
            lease_versions=versions,
            decided_by=decided_by,
            outcome=("delivered via cross-wing hand-over" if (completed and cross_wing)
                     else "delivered (single wing, no hand-over)" if completed
                     else f"NOT delivered ({final_status})"),
            cloud_llm_calls=llm_calls,
            request_to_lease_s=round(t_leased - t_submit, 3) if t_leased else None,
            completion_time_s=round(t_done - t_submit, 3) if completed else None,
            deadline_s=URGENT_DEADLINE_S,
            deadline_met=bool(completed and t_done and (t_done - t_submit) <= URGENT_DEADLINE_S),
        )
        return trial
    finally:
        for p in procs:
            try:
                p.send_signal(signal.SIGINT)
            except Exception:
                pass
        for p in procs:
            try:
                p.wait(5)
            except Exception:
                p.kill()
        if cloud_proc is not None:
            cloud_proc.terminate()
            try:
                cloud_proc.wait(8)
            except Exception:
                cloud_proc.kill()
        if not os.environ.get("NIMBUS_KEEP_DATA"):
            shutil.rmtree(data_dir, ignore_errors=True)


# --------------------------------------------------------------------------- aggregate + plots
def aggregate_and_plot() -> int:
    trials_path = RAW / "trials.jsonl"
    if not trials_path.exists():
        print("no trials to aggregate", file=sys.stderr)
        return 1
    trials = [json.loads(l) for l in trials_path.read_text().splitlines() if l.strip()]
    import csv as _csv
    keys = list(dict.fromkeys(k for t in trials for k in t if not isinstance(t[k], (dict, list))))
    with (OUT_DIR / "summary.csv").open("w", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        w.writerows(trials)
    env = {"experiment": "T5", "title": "Gate closed: cross-wing hand-over (real cloud)",
           "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "git_commit": _git_commit(), "host": os.uname().nodename if hasattr(os, "uname") else "?",
           "closed_corner": CLOSED_CORNER,
           "note": ("REAL run: C1/C2 use the deployed AWS endpoints (real Bedrock); C3 uses a local classical cloud "
                    "on the real hospital_sim6 pack. Edge = scripts/edge_sim.py (one per wing, kinematic); the east "
                    "client reports the closed store corner (east_south_hall_corner) via the real edge closure "
                    "contract, so the east wing can't reach the pickup and the job crosses wings. We measure whether "
                    "a cross-wing hand-over happened, the wing/leg sequence, who decided it (LLM vs rules), and "
                    "completion. No map/pack changes to the cloud -- the scenario uses the real map."),
           "trials": len(trials)}
    (OUT_DIR / "env.json").write_text(json.dumps(env, indent=1, default=str))
    _plots(trials)
    _readme(trials, env)
    print(f"[T5] wrote {OUT_DIR.relative_to(ROOT)} ({len(trials)} trials)")
    return 0


def _plots(trials: list) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as e:
        print(f"[T5] matplotlib unavailable, skipping plots: {e}", file=sys.stderr)
        return
    (OUT_DIR / "plots").mkdir(parents=True, exist_ok=True)
    colors = {"C1": "#1f77b4", "C2": "#ff7f0e", "C3": "#7f7f7f"}
    modes = [m for m in ("C1", "C2", "C3") if any(t["mode"] == m for t in trials)]
    fig, ax = plt.subplots()
    for i, m in enumerate(modes):
        t = next(t for t in trials if t["mode"] == m)
        ax.bar(i, t.get("completion_time_s") or 0, color=colors.get(m),
               hatch="" if t.get("cross_wing_handover") else "xx", edgecolor="black")
    ax.set_xticks(range(len(modes)))
    ax.set_xticklabels([f"{m}\n{'hand-over' if next(t for t in trials if t['mode']==m).get('cross_wing_handover') else 'no'}"
                        for m in modes])
    ax.set_ylabel("completion time (s)")
    ax.set_title("T5: cross-wing hand-over on the real cloud")
    for ext in ("png", "svg"):
        fig.savefig(OUT_DIR / "plots" / f"handover.{ext}", bbox_inches="tight")
    plt.close(fig)


def _readme(trials: list, env: dict) -> None:
    lines = ["# T5: Gate closed -- cross-wing hand-over (real cloud)", "",
             f"- Trials: {len(trials)}  |  generated {env['generated_utc']}  |  commit {env['git_commit']}",
             f"- Closed corner: `{env['closed_corner']}`", "",
             "## Scenario", "",
             "- Mission: `" + REQUEST_TEXT + "`. Pickup is the shared store, reachable only via the two south corners.",
             "- The east wing's corner (`" + CLOSED_CORNER + "`) is reported CLOSED by the edge, so the east wing "
             "cannot reach the pickup; the job must cross wings (the west wing collects, the pump reaches 2B via a "
             "hand-off between the wings).", "",
             "## Cloud (per project rule: C1/C2 always use the deployed endpoints)", "",
             "- **C1** -> deployed `qrgdidp9te` (real Bedrock).  **C2** -> deployed `xkczmnuqla`.  "
             "**C3** -> local classical cloud on the real `hospital_sim6` pack.",
             "- Edge: `scripts/edge_sim.py`, one per wing, kinematic; east reports the closed corner.", "",
             "## Expected contrast (ideal: C1 > C2 >> C3)", "",
             "- **C1 / C2**: the cloud (real Bedrock) arranges the cross-wing hand-over cleanly.",
             "- **C3**: the classical cloud reaches the hand-over only through its rules fallback.", "",
             env["note"], "", "## Plots", "", "![handover](plots/handover.png)", "",
             "## Results (per trial)", "",
             "| mode | cloud | outcome | cross-wing | wing sequence | decided by | llm | completion (s) | deadline |",
             "|---|---|---|---|---|---|---|---|---|"]
    for t in trials:
        lines.append("| {m} | {ck} | {o} | {x} | {ws} | {by} | {llm} | {c} | {dl} |".format(
            m=t["mode"], ck=t.get("cloud_kind"), o=t.get("outcome"),
            x="yes" if t.get("cross_wing_handover") else "no",
            ws=" -> ".join(t.get("wing_sequence") or []) or "-", by=t.get("decided_by"),
            llm=t.get("cloud_llm_calls"), c=t.get("completion_time_s"),
            dl="met" if t.get("deadline_met") else ("MISS" if t.get("completed") else "FAIL")))
    lines += ["", "Raw per-trial records are in `raw/trials.jsonl`; `summary.csv` is computed from them."]
    (OUT_DIR / "README.md").write_text("\n".join(lines) + "\n")


def _git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=str(ROOT)).decode().strip()
    except Exception:
        return "unknown"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["C1", "C2", "C3"], default="C1")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--plot-only", action="store_true")
    ap.add_argument("--reset", action="store_true")
    a = ap.parse_args()
    if a.plot_only:
        return aggregate_and_plot()
    if a.reset and RAW.exists():
        for p in RAW.glob("*"):
            p.unlink()
    trial = run_trial(a.mode, a.seed)
    RAW.mkdir(parents=True, exist_ok=True)
    with (RAW / "trials.jsonl").open("a") as f:
        f.write(json.dumps(trial, default=str) + "\n")
    print(f"[T5] {a.mode} seed={a.seed}: outcome={trial.get('outcome')} cross_wing={trial.get('cross_wing_handover')} "
          f"wings={trial.get('wing_sequence')} decided_by={trial.get('decided_by')} llm={trial.get('cloud_llm_calls')} "
          f"completion={trial.get('completion_time_s')}s")
    return 0 if trial.get("completed") else 2


if __name__ == "__main__":
    raise SystemExit(main())
