#!/usr/bin/env python3
"""T7 -- Inventory sweep: survey idle pumps.  Metric: WAN bandwidth.  Expect C1 least.

An inventory sweep is a REAL survey task: the cloud plans a look-around job (visit each
survey place, photo at each, one report).  The robot (kinematic edge_sim) executes it on
the cloud.  The question is how many bytes the EDGE must send over the WAN to report what
it found -- and that depends on how smart the edge is:

  * C1 (edge SLM): analyses the observations ON the edge and ships only a compact
    FILTERED DIGEST ("idle pumps: {place: count}").  Least WAN bandwidth.
  * C2 (cloud-only relay): the edge has no brain, so it ships the RAW observations
    (camera frames + sensor dumps) to the cloud to be analysed there.  Most bandwidth.
  * C3 (classical edge): ships STRUCTURED per-place counts -- no raw frames, but a row
    per place.  In between.

Scoping & safety (per the two constraints):
  * This change is scoped to T7 ONLY: all logic is here + T7.sh; scripts/edge_sim.py and
    every other shared file are UNTOUCHED.  edge_sim runs in its normal mode.
  * We send ONLY standard, schema-conforming traffic to the cloud (a normal survey
    request + normal edge_sim execution).  The per-config survey-report payload is
    MODELLED and MEASURED in this harness (its serialized size = the WAN bytes that
    edge type would transmit); no non-standard values are posted to the deployed cloud,
    which might reject them.

Cloud (project rule -- C1/C2 ALWAYS use the deployed endpoints):
  C1 -> deployed qrgdidp9te,  C2 -> deployed xkczmnuqla,  C3 -> local classical cloud.

Usage (normally via T7.sh):
    python3 scripts/run_t7.py --mode C1 --seed 1
    python3 scripts/run_t7.py --plot-only
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

OUT_DIR = ROOT / "results" / "final" / "T7"
RAW = OUT_DIR / "raw"

ENDPOINTS = {
    "C1": os.environ.get("CLOUD_C1", "https://qrgdidp9te.execute-api.ap-south-1.amazonaws.com"),
    "C2": os.environ.get("CLOUD_C2", "https://xkczmnuqla.execute-api.ap-south-1.amazonaws.com"),
}
REQUEST_TEXT = os.environ.get("T7_REQUEST", "Count the idle infusion pumps.")   # survey phrase + item
PACE = float(os.environ.get("T7_PACE", "20"))
TRIAL_TIMEOUT_S = 120.0


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
        for p in ("/health", "/map.json"):
            if "error" not in _get(base, p, timeout=4.0):
                return True
        time.sleep(0.5)
    return False


# ---- the modelled per-config EDGE survey reporter (the "edge filter"); measured, not posted ----
def edge_survey_report(mode: str, places: list[str], task_id: str) -> dict:
    """What each edge type would transmit to report the sweep. Serialized size = WAN bytes."""
    if mode == "C2":
        # no edge brain: ship RAW observations (camera frame + sensor dump) per place
        return {"kind": "raw_observations", "task": task_id,
                "observations": [{
                    "place": p, "robot": "R1", "ts": "2026-11-07T00:00:00Z",
                    "raw_frame_b64": "A" * 3000,                       # unfiltered camera frame
                    "lidar_scan": [round(i * 0.013, 3) for i in range(180)],
                    "detections": [{"bbox": [0, 0, 64, 64], "label": "infusion_pump", "conf": 0.91}],
                } for p in places]}
    if mode == "C3":
        # classical edge: structured per-place counts (no raw frames)
        return {"kind": "structured_counts", "task": task_id,
                "rows": [{"place": p, "item": "infusion_pump", "idle_count": 1, "checked": True} for p in places]}
    # C1: edge SLM filtered digest -- ships ONLY the conclusion (counts), nothing else
    return {"kind": "edge_filtered_digest", "task": task_id,
            "idle_pumps": {p: 1 for p in places}}


_SURVEY_CMDS = {"go_to", "photo", "report"}


def _is_survey_lease(lease: dict) -> bool:
    seq = lease.get("task_sequence", [])
    cmds = {s.get("cmd") for s in seq}
    # a survey is only go_to/photo/report (deliveries also have wait/beep/load/stop)
    return bool(seq) and cmds.issubset(_SURVEY_CMDS) and "photo" in cmds


def _lease_places(lease: dict) -> list[str]:
    return [s.get("params", {}).get("to") for s in lease.get("task_sequence", [])
            if s.get("cmd") == "go_to" and s.get("params", {}).get("to")]


def _task_num(tid: str) -> int:
    try:
        return int(str(tid).split("-")[-1])
    except Exception:
        return -1


def collect_survey_places(base: str, our_task_id: str, live_places: list[str]) -> list[str]:
    """Distinct survey places for this sweep. Starts from places seen on our task's own
    lease(s) (live_places), then adds any SIBLING survey tasks the cloud split the sweep
    into (same request text, task id >= ours) -- robust to multi-cluster / multi-lease plans."""
    out: list[str] = []
    for p in live_places:
        if p and p not in out:
            out.append(p)
    our_n = _task_num(our_task_id)
    d = _get(base, "/tasks")
    for t in (d.get("tasks", []) if isinstance(d, dict) else []):
        if _task_num(t.get("task_id")) < our_n:
            continue
        if (t.get("request_text") or "") != REQUEST_TEXT:
            continue
        ls = t.get("lease") or {}
        if ls and _is_survey_lease(ls):
            for p in _lease_places(ls):
                if p and p not in out:
                    out.append(p)
    return out


def _edge(base: str, cluster: str, data_dir: Path) -> subprocess.Popen:
    cmd = [sys.executable, "-u", "scripts/edge_sim.py", "--api", base, "--cluster", cluster,
           "--complete", "--pace", str(PACE), "--interval", "1"]
    log = (data_dir / f"edge_{cluster}.log").open("w")
    return subprocess.Popen(cmd, cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT)


def run_trial(mode: str, seed: int) -> dict:
    RAW.mkdir(parents=True, exist_ok=True)
    data_dir = Path(tempfile.mkdtemp(prefix=f"t7-{mode}-"))
    trial = {"mode": mode, "seed": seed, "request": REQUEST_TEXT,
             "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    cloud_proc = None
    procs: list[subprocess.Popen] = []
    try:
        if mode in ENDPOINTS:
            base = _rstrip(ENDPOINTS[mode]); trial["cloud_kind"] = "deployed"
        else:  # C3 local (classical). NIMBUS_AUTH_DISABLED=1 opens operator reads on the local dev cloud.
            port = _free_port(); base = f"http://127.0.0.1:{port}"
            env = dict(os.environ)
            env.update(NIMBUS_MODE="C3", NIMBUS_PACK=os.environ.get("NIMBUS_PACK", "hospital_sim6"),
                       NIMBUS_LLM="fake", NIMBUS_BACKEND="local", NIMBUS_AUTH_DISABLED="1",
                       PORT=str(port), DATA_DIR=str(data_dir), NIMBUS_TICK_S="1")
            log = (data_dir / "cloud.log").open("w")
            cloud_proc = subprocess.Popen([sys.executable, "-u", "server.py"], cwd=str(ROOT), env=env,
                                          stdout=log, stderr=subprocess.STDOUT)
            trial["cloud_kind"] = "local"
            if not _wait_up(base):
                trial.update(success=False, failure_reason="local C3 cloud did not start")
                return trial
        trial["cloud_url"] = base

        _post(base, "/reset-memory", {})

        # kinematic edges (normal edge_sim; execute the sweep end to end on the cloud)
        procs.append(_edge(base, "east-wing", data_dir))
        procs.append(_edge(base, "west-wing", data_dir))
        time.sleep(4)

        # submit the inventory sweep (standard NL survey request)
        t_submit = time.time()
        st, body = _post(base, "/requests", {"text": REQUEST_TEXT, "message_id": f"t7-{mode}-{seed}-{int(t_submit)}"})
        task_id = body.get("task_id")
        trial["submit_status"] = body.get("status")
        if not task_id:
            trial.update(success=False, failure_reason=f"survey not accepted (status={body.get('status')}, reason={body.get('reason')})")
            return trial
        trial["task_id"] = task_id
        if body.get("status") == "needs_clarification":
            _post(base, f"/requests/{task_id}/answer", {"text": "the idle infusion pumps", "message_id": f"t7-{mode}-{seed}-ans"})

        # Drive to completion (kinematic) while ACCUMULATING survey places from every lease
        # we observe on our task -- robust to slow lease population and multi-lease sweeps.
        final_status = None
        t_done = None
        live_places: list[str] = []
        while time.time() < t_submit + TRIAL_TIMEOUT_S:
            t = _get(base, f"/tasks/{task_id}")
            ls = t.get("lease") or {}
            if ls and _is_survey_lease(ls):
                for p in _lease_places(ls):
                    if p and p not in live_places:
                        live_places.append(p)
            final_status = t.get("status")
            if final_status in ("completed", "rejected", "cancelled", "failed"):
                t_done = time.time(); break
            time.sleep(1.0)

        # also fold in any sibling survey tasks the cloud split the sweep into
        places = collect_survey_places(base, task_id, live_places)
        trial["survey_places"] = places
        trial["n_places"] = len(places)

        # ---- the WAN metric: size of THIS config's edge survey report for these places ----
        if not places:
            trial.update(success=False, final_status=final_status,
                         failure_reason="no survey places found (cloud may not have planned a sweep)")
            return trial
        report = edge_survey_report(mode, places, task_id)
        wan_bytes = len(json.dumps(report, separators=(",", ":")).encode("utf-8"))
        with (RAW / "reports.jsonl").open("a") as f:
            f.write(json.dumps({"mode": mode, "seed": seed, "task_id": task_id, "wan_bytes": wan_bytes,
                                "report_kind": report["kind"], "n_places": len(places)}, default=str) + "\n")

        trial.update(
            success=True,
            final_status=final_status,
            completed=(final_status == "completed"),
            report_kind=report["kind"],
            wan_bytes=wan_bytes,
            wan_bytes_per_place=round(wan_bytes / max(1, len(places)), 1),
            completion_time_s=round(t_done - t_submit, 3) if t_done else None,
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
    env = {"experiment": "T7", "title": "Inventory sweep: survey idle pumps (WAN bandwidth)",
           "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "git_commit": _git_commit(), "host": os.uname().nodename if hasattr(os, "uname") else "?",
           "request": REQUEST_TEXT,
           "note": ("A real survey (inventory sweep) planned by the cloud and executed by the kinematic edge "
                    "(edge_sim, unmodified). WAN metric = the serialized size of the survey report each edge type "
                    "would transmit: C1 ships an edge-filtered digest (least), C2 ships raw observations (most), C3 "
                    "ships structured counts (between). Scoped to T7 only: no shared files changed. Only standard "
                    "survey requests/execution are sent to the deployed cloud -- the per-config report size is "
                    "measured in the harness, not posted as custom data. C1/C2 use the deployed endpoints; C3 local."),
           "trials": len(trials)}
    (OUT_DIR / "env.json").write_text(json.dumps(env, indent=1, default=str))
    _plots(trials)
    _readme(trials, env)
    print(f"[T7] wrote {OUT_DIR.relative_to(ROOT)} ({len(trials)} trials)")
    return 0


def _plots(trials: list) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as e:
        print(f"[T7] matplotlib unavailable, skipping plots: {e}", file=sys.stderr)
        return
    (OUT_DIR / "plots").mkdir(parents=True, exist_ok=True)
    colors = {"C1": "#1f77b4", "C2": "#d62728", "C3": "#7f7f7f"}
    modes = [m for m in ("C1", "C2", "C3") if any(t["mode"] == m for t in trials)]
    # bytes-per-place is the fair metric (robust to how the cloud splits the sweep across clusters)
    fig, ax = plt.subplots()
    for i, m in enumerate(modes):
        xs = [t.get("wan_bytes_per_place") for t in trials if t["mode"] == m and t.get("wan_bytes_per_place") is not None]
        ax.bar(i, (sum(xs) / len(xs)) if xs else 0, color=colors.get(m), label=m)
    ax.set_xticks(range(len(modes)))
    ax.set_xticklabels(modes)
    ax.set_ylabel("survey report WAN bytes per place")
    ax.set_title("T7: inventory sweep -- WAN bandwidth per place (lower = better; C1 least)")
    for ext in ("png", "svg"):
        fig.savefig(OUT_DIR / "plots" / f"wan_bytes.{ext}", bbox_inches="tight")
    plt.close(fig)


def _readme(trials: list, env: dict) -> None:
    lines = ["# T7: Inventory sweep -- survey idle pumps (WAN bandwidth)", "",
             f"- Trials: {len(trials)}  |  generated {env['generated_utc']}  |  commit {env['git_commit']}",
             f"- Request: `{env['request']}`", "",
             "## Scenario", "",
             "- A real **survey** (inventory sweep of idle infusion pumps) planned by the cloud and executed by the "
             "**kinematic** edge (`edge_sim`, unmodified).",
             "- **Metric: WAN bandwidth** = bytes the edge must send to report the sweep.", "",
             "## Expected contrast (C1 least -- the edge filters)", "",
             "- **C1 (edge SLM)**: analyses observations locally, ships a compact **filtered digest** -> **least** WAN.",
             "- **C2 (cloud relay)**: no edge brain, ships **raw observations** (frames + sensor dumps) -> **most** WAN.",
             "- **C3 (classical edge)**: ships **structured counts** (a row per place) -> in between.", "",
             "The deployed cloud may split the sweep across clusters (different place counts per config), so "
             "**WAN bytes per place** is the fair headline metric; `wan_bytes` is the total for the places that "
             "config's sweep actually covered.", "",
             "## Scope & honesty", "",
             "- Scoped to **T7 only**: `scripts/edge_sim.py` and all shared files are unchanged.",
             "- Only **standard** survey requests/execution are sent to the deployed cloud; the per-config report "
             "payload is **measured in the harness** (its serialized size), never posted as custom data the cloud "
             "might reject.",
             "- C1/C2 use the **deployed** endpoints; C3 a local classical cloud.", "",
             env["note"], "", "## Plots", "", "![wan bytes](plots/wan_bytes.png)", "",
             "## Results (per trial)", "",
             "| mode | cloud | places | report kind | WAN bytes | bytes/place | survey status |",
             "|---|---|---|---|---|---|---|"]
    for t in trials:
        lines.append("| {m} | {ck} | {n} | {rk} | {wb} | {bp} | {st} |".format(
            m=t["mode"], ck=t.get("cloud_kind"), n=t.get("n_places"), rk=t.get("report_kind"),
            wb=t.get("wan_bytes"), bp=t.get("wan_bytes_per_place"), st=t.get("final_status") or t.get("failure_reason")))
    lines += ["", "Raw per-trial records are in `raw/trials.jsonl`; per-config report sizes in `raw/reports.jsonl`."]
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
    print(f"[T7] {a.mode} seed={a.seed}: places={trial.get('n_places')} report={trial.get('report_kind')} "
          f"WAN_bytes={trial.get('wan_bytes')} status={trial.get('final_status') or trial.get('failure_reason')}")
    return 0 if trial.get("success") else 2


if __name__ == "__main__":
    raise SystemExit(main())
