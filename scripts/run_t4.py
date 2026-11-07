#!/usr/bin/env python3
"""T4 -- Blocked corridor; detour inside the lease zone (PRD Table 3).

One trial = one delivery through the REAL Nimbus workflow for one configuration
(C1/C2/C3). A persistent corridor obstacle forces a tactical decision, and a
detour exists INSIDE the lease's allowed zones. The cloud<->edge link is a BAD
link (degraded: latency + jitter + loss) for the whole mission -- it is up, just
poor. The question T4 answers is who is fastest to resolve the obstacle:

* C1 (edge SLM): decides the detour LOCALLY at the edge -- no cloud round trip --
  so the bad link does not touch the decision path. FASTEST.
* C2 (cloud-only relay): has no local brain, so every blocked step costs one cloud
  round trip (POST /relay/decide) over the bad link. It STALLS on the link --
  latency + jitter + retransmits on loss -- and is much slower (or, if the link is
  bad enough that the round trip times out, it escalates).
* C3 (deterministic rules): decides the detour LOCALLY with its finite-state rules,
  so it too is fast -- another classical-baseline point.

Method / honesty (documented in the generated README):
* Cloud: a LOCAL cloud this script launches (server.py) in the requested mode.

  WHY T4 USES A LOCAL CLOUD, NOT THE DEPLOYED (AWS) ENDPOINT:
    1. The scenario needs a controlled corridor blockage WITH a guaranteed detour
       INSIDE the lease zone -- a test-scoped map/scenario configuration (the
       compressed-timing pack packs/hospital_sim6_t2 and a deterministically placed
       obstacle) that is NOT part of the actual deployed hospital map. We must shape
       the map/scenario so the "blocked corridor, in-lease detour" case appears
       reliably; we cannot and must not reshape the deployed hospital map to do it.
    2. The bad link itself is injected by routing edge->cloud traffic through an
       in-process FaultProxy (scripts/fault_harness.py: pure-Python latency/jitter/
       loss, no tc/NET_ADMIN) -- which cannot be applied against a managed HTTPS
       endpoint from this host.
  Both reasons are test-scoped to this experiment. T1/T8 need no map/scenario change
  and no injected impairment, so they use the deployed C1/C2 endpoints.
* Edge: the REAL edge supervisor (edge/supervisor/supervisor.py) driven in-process,
  kinematic mock-robot backend (like T2). The obstacle is injected on its route.
* Timing is MEASURED: completion time (request->delivered) and the edge decision
  latency at the obstacle. C2's decision latency is the bad-link round trip; C1/C3
  decide locally (~0 link cost).

Usage (normally via T4.sh):
    python3 scripts/run_t4.py --mode C1 --profile severe --seed 1
    python3 scripts/run_t4.py --plot-only
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from edge.supervisor.supervisor import EdgeSupervisor  # noqa: E402
from fault_harness import FaultProxy, FaultProfile, PRESET_PROFILES  # noqa: E402

OUT_DIR = ROOT / "results" / "final" / "T4"
RAW = OUT_DIR / "raw"

REQUEST_TEXT = "Need a pediatric infusion pump at 2B-12, urgent."
URGENT_DEADLINE_S = 12 * 60
T4_PACK = os.environ.get("NIMBUS_PACK", "packs/hospital_sim6_t2")   # reuse T2's compressed-timing pack
OBSTACLE_ZONE = os.environ.get("T4_OBSTACLE_ZONE", "east_mid_hall")
OBSTACLE_DESC = "Maintenance trolley blocking path"
CLUSTER = "east-wing"
TRIAL_TIMEOUT_S = 240.0
DEFAULT_PROFILE = os.environ.get("T4_PROFILE", "severe")            # the "bad link" for C2


# --------------------------------------------------------------------------- http helpers
def _get(url: str, timeout: float = 6.0):
    t0 = time.time()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = r.read()
            return True, r.status, data, (time.time() - t0) * 1000.0
    except Exception:
        return False, 0, b"", (time.time() - t0) * 1000.0


def _post(url: str, body: dict, timeout: float = 12.0):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
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


def _profile(name: str) -> FaultProfile:
    if name in ("healthy", "none", "clear"):
        return PRESET_PROFILES["healthy"]
    if name in PRESET_PROFILES:
        return PRESET_PROFILES[name]
    # custom "lat:jitter:loss", e.g. "800:200:0.1"
    try:
        lat, jit, loss = (name.split(":") + ["0", "0"])[:3]
        return FaultProfile(name="custom", latency_ms=float(lat), jitter_ms=float(jit), loss_rate=float(loss))
    except Exception:
        return PRESET_PROFILES["severe"]


# --------------------------------------------------------------------------- cloud
def launch_cloud(mode: str, port: int, data_dir: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env.update(NIMBUS_MODE=mode, NIMBUS_PACK=T4_PACK,
               NIMBUS_LLM=env.get("NIMBUS_LLM", "fake"), NIMBUS_BACKEND="local",
               NIMBUS_AUTH_DISABLED="1",   # local dev cloud: fail-closed auth would 401 /fleet,/tasks,...
               PORT=str(port), DATA_DIR=str(data_dir), NIMBUS_TICK_S=env.get("NIMBUS_TICK_S", "1"))
    log = (data_dir / "cloud.log").open("w")
    return subprocess.Popen([sys.executable, "-u", "server.py"], cwd=str(ROOT), env=env, stdout=log, stderr=log)


def wait_up(base: str, timeout: float = 30.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if _get(base + "/fleet", timeout=3.0)[0]:
            return True
        time.sleep(0.5)
    return False


# --------------------------------------------------------------------------- one trial
def run_trial(mode: str, profile_name: str, seed: int) -> dict:
    RAW.mkdir(parents=True, exist_ok=True)
    cloud_port = _free_port()
    proxy_port = _free_port()
    cloud_base = f"http://127.0.0.1:{cloud_port}"
    proxy_base = f"http://127.0.0.1:{proxy_port}"       # the edge talks to the cloud THROUGH here (bad link)
    data_dir = Path(tempfile.mkdtemp(prefix=f"t4-{mode}-"))
    prof = _profile(profile_name)
    trial = {"mode": mode, "profile": profile_name, "seed": seed,
             "link": {"latency_ms": prof.latency_ms, "jitter_ms": prof.jitter_ms, "loss_rate": prof.loss_rate},
             "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    cloud = launch_cloud(mode, cloud_port, data_dir)
    proxy = FaultProxy(listen_host="127.0.0.1", listen_port=proxy_port,
                       target_host="127.0.0.1", target_port=cloud_port, profile=prof)
    try:
        if not wait_up(cloud_base):
            trial.update(success=False, failure_reason="cloud did not start")
            return trial
        try:
            trial["cloud"] = {k: json.loads(_get(cloud_base + "/fleet")[2]).get(k)
                              for k in ("mode", "scheduler", "llm", "pack")}
        except Exception:
            trial["cloud"] = {}
        proxy.start()

        # REAL edge supervisor (kinematic), pointed at the bad-link proxy.
        sup = EdgeSupervisor(
            cluster_id=CLUSTER, cloud_url=proxy_base, db_path=str(data_dir / "edge.db"),
            slm_mode="mock", system_mode=mode, robot_backend="mock", verbose=True,
        )
        for r in sup.robots.values():
            r.inject_obstacle(OBSTACLE_ZONE, obstacle_type="static", description=OBSTACLE_DESC)

        # submit the mission to the cloud DIRECTLY (request ingress is not the link under test;
        # the edge<->cloud execution path is). Answer any clarification so it proceeds unattended.
        t_submit = time.time()
        st, body = _post(cloud_base + "/requests", {"text": REQUEST_TEXT, "message_id": f"t4-{mode}-{seed}"})
        task_id = body.get("task_id")
        if body.get("status") == "needs_clarification" and task_id:
            _post(cloud_base + f"/requests/{task_id}/answer",
                  {"text": "Yes, a paediatric pump.", "message_id": f"t4-{mode}-{seed}-ans"})

        # the edge pulls the lease THROUGH the bad link, then executes it THROUGH the bad link.
        lease = None
        end_fetch = time.time() + 40.0
        while time.time() < end_fetch and lease is None:
            try:
                for cand in sup.fetch_leases_from_cloud():
                    if cand.get("cluster_id") == CLUSTER:
                        lease = cand
                        break
            except Exception:
                pass
            if lease is None:
                time.sleep(0.5)
        if lease is None:
            trial.update(success=False, failure_reason=f"no lease for {CLUSTER} within 40s (status={body.get('status')})")
            return trial
        t_leased = time.time()

        final_box: dict = {}

        def run_mission():
            try:
                final_box["result"] = sup.process_lease(lease, verbose=True)
            except Exception as exc:
                final_box["result"] = {"status": "ERROR", "reason": str(exc)}

        mt = threading.Thread(target=run_mission, daemon=True)
        mt.start()
        mt.join(TRIAL_TIMEOUT_S)
        final = final_box.get("result") or {"status": "TIMEOUT", "reason": "mission did not finish in time"}
        t_done = time.time()

        status = final.get("status")
        completed = status == "COMPLETED"

        # ---- edge decision telemetry: the obstacle decision latency, per config ----
        decisions = _read_decisions(sup, lease.get("lease_id"))
        for d in decisions:
            with (RAW / "decision_log.jsonl").open("a") as f:
                f.write(json.dumps({"mode": mode, "profile": profile_name, "seed": seed, **d}, default=str) + "\n")
        obstacle_decisions = [d for d in decisions if d.get("action") in
                              ("LOCAL_DETOUR", "LOCAL_WAIT", "ESCALATE_UNEXPECTED", "DISPATCH_STEP")]
        decision_latencies = [float(d.get("latency_ms") or 0.0) for d in decisions]
        max_decision_ms = max(decision_latencies) if decision_latencies else 0.0
        sum_decision_ms = sum(decision_latencies)

        pstats = dict(proxy.stats)
        _OUTCOME = {
            "COMPLETED": "delivered",
            "ESCALATED_UNEXPECTED": "STALLED -> escalated (relay could not resolve over the bad link)",
            "EXPIRED_PARKED": "STALLED -> lease expired, robot parked",
            "TIMEOUT": "STALLED -> mission did not finish",
        }
        trial.update(
            success=completed,
            final_status=status,
            outcome=_OUTCOME.get(status, status),
            decided_by=(decisions[0].get("decided_by") if decisions else None),
            task_id=task_id,
            request_to_lease_s=round(t_leased - t_submit, 3),
            completion_time_s=round(t_done - t_submit, 3) if completed else None,
            time_to_outcome_s=round(t_done - t_submit, 3),
            robot_execution_s=round(t_done - t_leased, 3),
            deadline_s=URGENT_DEADLINE_S,
            deadline_met=bool(completed and (t_done - t_submit) <= URGENT_DEADLINE_S),
            # the headline: who paid the bad link, and how much
            obstacle_decisions=len(obstacle_decisions),
            max_decision_latency_ms=round(max_decision_ms, 1),
            total_decision_latency_ms=round(sum_decision_ms, 1),
            link_bytes_forwarded=pstats.get("bytes_forwarded"),
            link_packets_dropped=pstats.get("packets_dropped"),
            failure_reason=None if completed else final.get("reason"),
        )
        return trial
    finally:
        try:
            proxy.stop()
        except Exception:
            pass
        cloud.terminate()
        try:
            cloud.wait(10)
        except Exception:
            cloud.kill()
        if not os.environ.get("NIMBUS_KEEP_DATA"):
            shutil.rmtree(data_dir, ignore_errors=True)


def _read_decisions(sup, lease_id: str | None) -> list[dict]:
    """Pull the edge tactical decisions (with latency) this trial logged to the edge store."""
    out: list[dict] = []
    try:
        rows = sup.store.get_decisions(lease_id) if hasattr(sup.store, "get_decisions") else []
    except Exception:
        rows = []
    for r in rows or []:
        out.append({
            "decided_by": r.get("decided_by"),
            "action": (r.get("decision_output") or {}).get("action") if isinstance(r.get("decision_output"), dict) else r.get("decision_action"),
            "latency_ms": r.get("latency_ms"),
            "reason": (r.get("decision_output") or {}).get("reason") if isinstance(r.get("decision_output"), dict) else None,
        })
    return out


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

    env = {"experiment": "T4", "title": "Blocked corridor; detour inside lease zone",
           "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "git_commit": _git_commit(), "host": os.uname().nodename if hasattr(os, "uname") else "?",
           "pack": T4_PACK, "obstacle_zone": OBSTACLE_ZONE,
           "uses_deployed_endpoint": False,
           "why_local_cloud": ("T4 uses a LOCAL cloud, not the deployed endpoint, for two test-scoped reasons: "
                    "(1) the 'blocked corridor with a detour INSIDE the lease zone' case needs a controlled "
                    "map/scenario configuration (a compressed-timing pack and a deterministically placed obstacle) "
                    "that is NOT part of the actual deployed hospital map -- the detour scenario requires custom "
                    "map/scenario changes, so we run it locally rather than reshape the real hospital map; and "
                    "(2) the bad link is injected with an in-process FaultProxy, which cannot be applied against a "
                    "managed HTTPS endpoint from this host. T1/T8 need neither and use the deployed C1/C2 endpoints."),
           "note": ("Edge = the real supervisor (C1 SLM / C2 relay / C3 rules), kinematic mock robot, driven "
                    "in-process. A persistent corridor obstacle forces a tactical decision; a detour exists "
                    "inside the lease. The edge<->cloud link is degraded for the whole mission by an in-process "
                    "FaultProxy (latency+jitter+loss), not tc. C1 and C3 decide the detour LOCALLY (link not on "
                    "the decision path) and are fast; C2 must round-trip to the cloud per decision and stalls on "
                    "the bad link. Completion time and decision latency are measured, not inferred."),
           "trials": len(trials)}
    (OUT_DIR / "env.json").write_text(json.dumps(env, indent=1, default=str))

    _plots(trials)
    _readme(trials, env)
    print(f"[T4] wrote {OUT_DIR.relative_to(ROOT)} ({len(trials)} trials)")
    return 0


def _plots(trials: list) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as e:
        print(f"[T4] matplotlib unavailable, skipping plots: {e}", file=sys.stderr)
        return
    (OUT_DIR / "plots").mkdir(parents=True, exist_ok=True)
    colors = {"C1": "#1f77b4", "C2": "#d62728", "C3": "#7f7f7f"}
    modes = [m for m in ("C1", "C2", "C3") if any(t["mode"] == m for t in trials)]

    def bar(metric, ylabel, fname):
        fig, ax = plt.subplots()
        for i, m in enumerate(modes):
            ts = [t.get(metric) for t in trials if t["mode"] == m and t.get(metric) is not None]
            val = (sum(ts) / len(ts)) if ts else 0.0
            ax.bar(i, val, color=colors.get(m), label=m)
        ax.set_xticks(range(len(modes)))
        ax.set_xticklabels(modes)
        ax.set_ylabel(ylabel)
        ax.set_title(fname.replace("_", " ").title())
        for ext in ("png", "svg"):
            fig.savefig(OUT_DIR / "plots" / f"{fname}.{ext}", bbox_inches="tight")
        plt.close(fig)

    bar("time_to_outcome_s", "time to outcome (s)", "completion_time")
    bar("max_decision_latency_ms", "obstacle decision latency (ms)", "decision_latency")


def _readme(trials: list, env: dict) -> None:
    lines = ["# T4: Blocked corridor; detour inside the lease zone", "",
             f"- Trials: {len(trials)}  |  generated {env['generated_utc']}  |  commit {env['git_commit']}",
             f"- Pack: `{env['pack']}`  |  obstacle: `{env['obstacle_zone']}`", "",
             "## Scenario", "",
             "- Mission: `" + REQUEST_TEXT + "` through the real request -> allocation -> checker -> lease -> edge path.",
             "- A persistent corridor obstacle blocks the route; a valid detour exists INSIDE the lease's zones.",
             "- The edge<->cloud link is a BAD link (degraded latency/jitter/loss) for the whole mission.", "",
             "## Expected contrast", "",
             "- **C1**: edge SLM decides the detour locally -> the bad link is not on the decision path -> **fastest**.",
             "- **C2**: cloud-only relay pays one cloud round trip per blocked step over the bad link -> **stalls on "
             "the link** (slowest; escalates if the round trip can't resolve).",
             "- **C3**: local finite-state rules detour -> also fast (another classical-baseline point).", "",
             "## Why T4 uses a local cloud, not the deployed AWS endpoint", "",
             "T1 and T8 run C1/C2 against the deployed clouds. **T4 does not**, for two test-scoped reasons:", "",
             "1. **The detour scenario needs custom map/scenario changes not in the real hospital map.** T4 requires a "
             "blocked corridor that still has a valid detour *inside the lease zone*; staging that reliably uses a "
             "test-scoped configuration (a compressed-timing pack and a deterministically placed obstacle) that is "
             "not part of the actual deployed hospital map. We shape the scenario locally rather than reshape the "
             "real hospital map.",
             "2. **The bad link can't be injected against a managed endpoint.** The degradation is an in-process "
             "FaultProxy (latency/jitter/loss), which cannot be applied to a managed HTTPS endpoint from this host "
             "(no tc/NET_ADMIN).", "",
             env.get("why_local_cloud", ""), "",
             env["note"], "", "## Plots", "",
             "![completion time](plots/completion_time.png)",
             "![decision latency](plots/decision_latency.png)", "",
             "## Results (per trial)", "",
             "| mode | profile | outcome | time-to-outcome (s) | obstacle decision latency (ms) | decided by | deadline |",
             "|---|---|---|---|---|---|---|"]
    for t in trials:
        lines.append("| {m} | {p} | {o} | {tto} | {lat} | {by} | {dl} |".format(
            m=t["mode"], p=t.get("profile"), o=t.get("outcome"), tto=t.get("time_to_outcome_s"),
            lat=t.get("max_decision_latency_ms"), by=t.get("decided_by"),
            dl="met" if t.get("deadline_met") else ("MISS" if t.get("success") else "FAIL")))
    lines += ["", "Raw per-trial records are in `raw/trials.jsonl`; `summary.csv` is computed from them.",
              "Per-decision edge telemetry (with latency) is in `raw/decision_log.jsonl`."]
    (OUT_DIR / "README.md").write_text("\n".join(lines) + "\n")


def _git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=str(ROOT)).decode().strip()
    except Exception:
        return "unknown"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["C1", "C2", "C3"], default="C1")
    ap.add_argument("--profile", default=DEFAULT_PROFILE,
                    help="bad-link profile: healthy|mild_wan|shielded_room|severe or custom 'lat:jitter:loss'")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--plot-only", action="store_true")
    ap.add_argument("--reset", action="store_true")
    a = ap.parse_args()

    if a.plot_only:
        return aggregate_and_plot()

    if a.reset and RAW.exists():
        for p in RAW.glob("*"):
            p.unlink()

    trial = run_trial(a.mode, a.profile, a.seed)
    RAW.mkdir(parents=True, exist_ok=True)
    with (RAW / "trials.jsonl").open("a") as f:
        f.write(json.dumps(trial, default=str) + "\n")
    print(f"[T4] {a.mode}/{a.profile} seed={a.seed}: outcome={trial.get('outcome')} "
          f"time_to_outcome={trial.get('time_to_outcome_s')}s "
          f"decision_latency_ms={trial.get('max_decision_latency_ms')} decided_by={trial.get('decided_by')}")
    return 0 if trial.get("final_status") else 2


if __name__ == "__main__":
    raise SystemExit(main())
