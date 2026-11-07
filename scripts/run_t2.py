#!/usr/bin/env python3
"""T2 -- Urgent delivery under a sustained WAN outage (PRD Table 3, E3/E4).

One trial = one urgent equipment delivery driven end to end through the REAL
Nimbus workflow for one configuration (C1/C2/C3), with the cloud<->edge WAN cut
right after the lease is granted and held to the end of the mission, while a
corridor obstacle forces a tactical decision mid-route.

What the outage is designed to reveal (this is the whole point of T2):

* C1 (edge SLM): when the robot hits the blocked corridor, the edge supervisor's
  local SLM takes over, picks a permitted detour, and COMPLETES the delivery with
  no cloud contact.
* C2 (cloud-only relay): the edge has no local brain.  At the obstacle it must ask
  the cloud (POST /relay/decide); the WAN is down, so every round trip fails.  It
  can only LOCAL_WAIT and retry.  Check-ins never reach the cloud, the lease is
  never renewed, and when it expires the controller SAFE-PARKS the robot ->
  EXPIRED_PARKED.  The urgent delivery FAILS.
* C3 (deterministic rules): the edge runs a small finite-state rule set locally
  (wait once, one map-valid detour, else operator), so on a detour-able obstacle it
  COMPLETES -- a classical baseline that happens to cope with this particular event.

Design choices forced by this host and by reproducibility (documented in the
generated README so nothing is silently substituted):

* Cloud: a LOCAL cloud this script launches (server.py) in the requested
  NIMBUS_MODE.  C2 is a real C2 cloud (NIMBUS_MODE=C2, /relay/decide live) --
  relay requests never silently go to a C1 endpoint.

  WHY T2 CANNOT USE THE DEPLOYED (AWS) ENDPOINT -- unlike T1 and T8, which do:
    0. T2 is fundamentally a NETWORK-DISRUPTION SIMULATION: its whole purpose is to
       cut and restore the cloud<->edge link on a controlled schedule.  That network
       disruption cannot be applied to a managed AWS endpoint from this host (no tc/
       iptables), so T2 simulates it against a local cloud it controls.
    1. The whole experiment IS a precisely-timed WAN cut.  T2 creates the outage
       by SIGSTOP-ing the cloud *process* the instant the lease is ingested and
       SIGCONT-ing it at mission end.  The deployed cloud is a managed service
       (API Gateway v2 + Fargate); you cannot pause its process, and you cannot
       sever only the cloud<->edge link at a chosen millisecond without it.  A
       local cloud is the only way to own that process and cut it deterministically.
    2. The contrast needs a short lease so C2's offline stall reaches a terminal
       state in ~15 s instead of ~15 min.  T2 uses a compressed-timing pack
       (packs/hospital_sim6_t2: 1 min urgent deadline + 8 s grace); the deployed
       cloud is fixed to the production pack and timing, which we must not mutate.
    3. T2 must drive the REAL edge supervisor in-process (to inject the obstacle
       and read the EXPIRED_PARKED / ESCALATED outcome directly); that supervisor
       must point at a cloud whose process this script controls.
    4. The run would otherwise write dozens of throwaway urgent tasks and a forced
       outage onto the shared deployed cloud other configs (T1/T8) depend on.
  T1 and T8 have no outage and no timing compression, so they DO use the deployed
  C1/C2 endpoints (requests show in the operator GUI); T2 is the one experiment
  whose mechanism is incompatible with a managed endpoint.
* Edge: the REAL edge supervisor (edge/supervisor/supervisor.py) driven in-process
  -- it reads leases, checks in, renews, runs tactical autonomy (C1 SLM / C2 relay
  / C3 rules), and safe-parks on lease expiry.  Not a scripted walker.  The robot
  is the in-memory MockRobot backend; the obstacle is injected on its route.
* Outage: the cloud<->edge link is cut by SIGSTOP-ing the local cloud the instant
  the supervisor has ingested the lease, and held until the mission ends (then
  SIGCONT).  A real, measured cut that touches only cloud<->edge; the robot<->edge
  path is internal to the supervisor and never impaired.  If `tc`+NET_ADMIN and
  --iface are present, netem link shaping is also applied; this host has no tc, so
  that path is skipped and reported, never faked.
* Timing: the stock urgent lease lives ~15 min (12 min deadline + 180 s grace), so
  C2 would stall that long before parking.  T2 uses a compressed-timing pack
  (packs/hospital_sim6_t2: 1 min urgent deadline + 8 s grace) so a trial finishes
  in ~1-2 min.  The mechanism is identical at production timescales, only faster.
* Network metrics are MEASURED, not inferred: RTT is the elapsed time of timed HTTP
  probes; loss is the fraction of probes that failed (the outage window shows up as
  a loss spike); recovery is measured from the first probe that succeeds after the
  cut is lifted; background throughput is bytes actually pulled by the generator.

Usage (normally invoked by T2.sh):
    python3 scripts/run_t2.py --mode C1 --seed 1
    python3 scripts/run_t2.py --plot-only          # aggregate raw/ -> summary + plots
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
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "experiments" / "cloud"))

from edge.supervisor.supervisor import EdgeSupervisor  # noqa: E402

OUT_DIR = ROOT / "results" / "final" / "T2"
RAW = OUT_DIR / "raw"

# The T2 mission: the T1 urgent delivery (paediatric infusion pump to ward 2B).
REQUEST_TEXT = "Need a pediatric infusion pump at 2B-12, urgent."
URGENT_DEADLINE_S = 12 * 60          # the PRD urgent SLA we score against (not the compressed lease clock)
T2_PACK = os.environ.get("NIMBUS_PACK", "packs/hospital_sim6_t2")
OBSTACLE_ZONE = os.environ.get("T2_OBSTACLE_ZONE", "east_mid_hall")
OBSTACLE_DESC = "Maintenance trolley blocking path"
CLUSTER = "east-wing"
POST_LEASE_CUT_DELAY_S = 1.5         # let the initial check-in through, then cut -> "right after lease"
TRIAL_TIMEOUT_S = 240.0


# --------------------------------------------------------------------------- http helpers
def _get(url: str, timeout: float = 4.0):
    """Timed GET. Returns (ok, status, body_bytes, rtt_ms)."""
    t0 = time.time()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = r.read()
            return True, r.status, data, (time.time() - t0) * 1000.0
    except Exception:
        return False, 0, b"", (time.time() - t0) * 1000.0


def _post(url: str, body: dict, timeout: float = 8.0):
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


# --------------------------------------------------------------------------- background threads
class Sampler(threading.Thread):
    """Independently MEASURES the cloud<->edge link by timed HTTP probes to a
    read-only endpoint (GET /fleet). Records RTT and success/failure over time;
    mutates no state. The outage window shows up as a run of failed probes."""

    def __init__(self, base: str, interval: float = 0.5):
        super().__init__(daemon=True)
        self.base, self.interval = base, interval
        self.samples: list[dict] = []
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            ok, _, data, rtt = _get(self.base + "/fleet")
            self.samples.append({"t": time.time(), "ok": ok, "rtt_ms": round(rtt, 2), "bytes": len(data)})
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()


class BackgroundTraffic(threading.Thread):
    """Concurrent background load against a SAFE read-only endpoint (GET /fleet):
    Nimbus-shaped cloud API chatter that pulls bytes without creating tasks or
    touching leases. Measures throughput (bytes actually transferred)."""

    def __init__(self, base: str, logf, workers: int = 4, rate_hz: float = 20.0):
        super().__init__(daemon=True)
        self.base, self.logf, self.workers, self.rate = base, logf, workers, rate_hz
        self.bytes = 0
        self.requests = 0
        self.started = None
        self.ended = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def _worker(self):
        gap = self.workers / self.rate if self.rate > 0 else 0
        while not self._stop.is_set():
            ok, _, data, rtt = _get(self.base + "/fleet", timeout=3.0)
            with self._lock:
                self.requests += 1
                self.bytes += len(data)
            self.logf.write(f"{time.time():.3f} GET /fleet ok={int(ok)} bytes={len(data)} rtt_ms={rtt:.1f}\n")
            self._stop.wait(gap)

    def run(self):
        self.started = time.time()
        ts = [threading.Thread(target=self._worker, daemon=True) for _ in range(self.workers)]
        for t in ts:
            t.start()
        self._stop.wait()
        self.ended = time.time()

    def stop(self):
        self._stop.set()

    def throughput_mbps(self) -> float:
        dur = (self.ended or time.time()) - (self.started or time.time())
        return (self.bytes * 8 / 1e6) / dur if dur > 0 else 0.0


# --------------------------------------------------------------------------- cloud process
def launch_cloud(mode: str, port: int, data_dir: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env.update(NIMBUS_MODE=mode, NIMBUS_PACK=T2_PACK,
               NIMBUS_LLM=env.get("NIMBUS_LLM", "fake"), NIMBUS_BACKEND="local",
               PORT=str(port), DATA_DIR=str(data_dir), NIMBUS_TICK_S=env.get("NIMBUS_TICK_S", "1"))
    log = (data_dir / "cloud.log").open("w")
    return subprocess.Popen([sys.executable, "-u", "server.py"], cwd=str(ROOT), env=env, stdout=log, stderr=log)


def wait_up(base: str, timeout: float = 30.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if _get(base + "/fleet", timeout=2.0)[0]:
            return True
        time.sleep(0.5)
    return False


# --------------------------------------------------------------------------- one trial
def run_trial(mode: str, seed: int, iface: str | None) -> dict:
    RAW.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    data_dir = Path(tempfile.mkdtemp(prefix=f"t2-{mode}-"))
    trial = {"mode": mode, "profile": "outage", "seed": seed,
             "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    netem_applied = False

    cloud = launch_cloud(mode, port, data_dir)
    try:
        if not wait_up(base):
            trial.update(success=False, failure_reason="cloud did not start")
            return trial
        fleet = _get(base + "/fleet")[2]
        try:
            trial["cloud"] = {k: json.loads(fleet).get(k) for k in ("mode", "scheduler", "llm", "pack")}
        except Exception:
            trial["cloud"] = {}

        # optional real link shaping, only where the kernel tools exist (not on this host)
        if iface and shutil.which("tc"):
            subprocess.run([str(ROOT / "netem.sh"), "mild_wan", iface], check=False)
            netem_applied = True
        trial["netem_applied"] = netem_applied

        # background load + independent RTT sampler (measure the link through the outage)
        traffic_log = (RAW / "traffic_generator.log").open("a")
        traffic_log.write(f"# trial mode={mode} seed={seed} @ {trial['started_utc']}\n")
        bg = BackgroundTraffic(base, traffic_log)
        sampler = Sampler(base)
        bg.start()
        sampler.start()

        # REAL edge supervisor (east wing), driven in-process so we can inject the
        # obstacle and cut the WAN at exact moments.
        sup = EdgeSupervisor(
            cluster_id=CLUSTER, cloud_url=base, db_path=str(data_dir / "edge.db"),
            slm_mode="mock",            # deterministic local reasoning, no network to ollama
            system_mode=mode, robot_backend="mock", verbose=True,
        )
        # inject a persistent corridor obstacle on every east robot's route: whichever
        # robot the cloud assigns will hit it mid-mission and must make a tactical call.
        for r in sup.robots.values():
            r.inject_obstacle(OBSTACLE_ZONE, obstacle_type="static", description=OBSTACLE_DESC)

        outage = {"start": None, "end": None, "recovered": None}

        # the urgent mission
        t_submit = time.time()
        st, body = _post(base + "/requests", {"text": REQUEST_TEXT, "message_id": f"t2-{mode}-{seed}"})
        task_id = body.get("task_id")
        if body.get("status") == "needs_clarification" and task_id:
            _post(base + f"/requests/{task_id}/answer",
                  {"text": "Yes, a paediatric pump.", "message_id": f"t2-{mode}-{seed}-ans"})

        # Deterministic "outage right after lease": while the cloud is still up, pull the
        # lease onto the edge (fetch_leases_from_cloud). Then cut the WAN and only then run
        # execution -- so every cloud interaction during the mission (check-in, renewal,
        # C2 relay) fails. No wall-clock race against the (instant) mock robot.
        lease = None
        t_leased = None
        end_fetch = time.time() + 30.0
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
            trial.update(success=False, failure_reason=f"no lease for {CLUSTER} within 30s (status={body.get('status')})")
            bg.stop(); sampler.stop(); traffic_log.close()
            return trial
        t_leased = time.time()

        # ---- CUT THE WAN (held to end of mission) ----
        outage["start"] = time.time()
        try:
            cloud.send_signal(signal.SIGSTOP)
        except Exception:
            pass

        # run the mission with the cloud unreachable; guard with a timeout thread
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

        # ---- LIFT THE OUTAGE; measure recovery from the first probe that succeeds ----
        try:
            cloud.send_signal(signal.SIGCONT)
        except Exception:
            pass
        outage["end"] = time.time()
        time.sleep(1.0)
        for s in sampler.samples:
            if s["t"] >= outage["end"] and s["ok"]:
                outage["recovered"] = s["t"]
                break

        bg.stop(); sampler.stop()
        time.sleep(0.5)
        traffic_log.close()

        status = (final or {}).get("status")
        completed = status == "COMPLETED"
        # every non-delivery here is a failed urgent delivery under the outage. We keep the
        # specific terminal state so the report can name the exact failure mode per config.
        _OUTCOME = {
            "COMPLETED": "delivered",
            "EXPIRED_PARKED": "PARKED (lease expired, robot safe-parked) - delivery FAILED",
            "ESCALATED_UNEXPECTED": "HALTED (no local brain; escalated to operator, but WAN down -> "
                                    "escalation stuck in offline outbox) - delivery FAILED",
            "ESCALATED_CLOUD": "HALTED (escalated to cloud; WAN down -> undeliverable) - delivery FAILED",
            "FAILED": "FAILED",
            "FAILED_BOUNDARY": "FAILED (detour left lease boundary)",
            "TIMEOUT": "FAILED (mission did not finish)",
            "ERROR": "ERROR",
        }
        parked = status == "EXPIRED_PARKED"
        failed = (not completed)

        # ---- cloud-side metrics (after SIGCONT so the cloud can answer) --------------------
        cost = json.loads(_get(base + "/cost")[2] or "{}")
        metrics = json.loads(_get(base + "/metrics")[2] or "{}")
        trace = json.loads(_get(base + f"/tasks/{task_id}/trace")[2] or "{}") if task_id else {}
        events = trace.get("events", [])
        for e in events:
            with (RAW / "decision_log.jsonl").open("a") as f:
                f.write(json.dumps({"mode": mode, "seed": seed, "task_id": task_id, **e}, default=str) + "\n")

        # network samples -> csv + measured values
        _append_network_csv(trial, sampler.samples, outage)
        pre = [s["rtt_ms"] for s in sampler.samples if s["ok"]
               and (not outage["start"] or s["t"] < outage["start"])]
        n = len(sampler.samples)
        failed_probes = sum(1 for s in sampler.samples if not s["ok"])

        trial.update(
            success=completed,
            parked=parked,
            failed=failed,
            final_status=status,
            outcome=_OUTCOME.get(status, f"FAILED ({status})"),
            task_id=task_id,
            request_to_lease_s=round(t_leased - t_submit, 3) if t_leased else None,
            completion_time_s=round(t_done - t_submit, 3) if completed else None,
            time_to_fail_s=round(t_done - t_submit, 3) if failed else None,
            deadline_s=URGENT_DEADLINE_S,
            deadline_met=bool(completed and (t_done - t_submit) <= URGENT_DEADLINE_S),
            failure_reason=None if completed else (final or {}).get("reason"),
            # network (measured, not requested)
            rtt_ms_mean=round(sum(pre) / len(pre), 2) if pre else None,
            probes=n,
            loss_pct=round(100.0 * failed_probes / n, 2) if n else None,
            bg_throughput_mbps=round(bg.throughput_mbps(), 3),
            bg_requests=bg.requests,
            bg_bytes=bg.bytes,
            outage_duration_s=round(outage["end"] - outage["start"], 2) if outage["start"] and outage["end"] else 0.0,
            recovery_time_s=round(outage["recovered"] - outage["end"], 2) if outage.get("recovered") and outage["end"] else None,
            # distributed system (measured from the decision log / cost)
            cloud_llm_calls=_tally_llm(events, cost),
            cost_snapshot=cost.get("counters", cost),
            metrics_snapshot={"gauges": metrics.get("gauges")},
        )
        return trial
    finally:
        if netem_applied and iface:
            subprocess.run([str(ROOT / "netem.sh"), "clear", iface], check=False)
        try:
            cloud.send_signal(signal.SIGCONT)
        except Exception:
            pass
        cloud.terminate()
        try:
            cloud.wait(10)
        except Exception:
            cloud.kill()
        if not os.environ.get("NIMBUS_KEEP_DATA"):
            shutil.rmtree(data_dir, ignore_errors=True)


def _tally_llm(events: list, cost: dict) -> int:
    n = 0
    for e in events:
        blob = json.dumps(e).lower()
        if '"llm"' in blob and '"calls"' in blob:
            try:
                n += int(e.get("output", {}).get("llm", {}).get("calls", 0))
            except Exception:
                pass
    if n == 0:
        c = cost.get("counters", cost)
        n = int(c.get("llm_calls", 0)) if isinstance(c, dict) else 0
    return n


def _append_network_csv(trial: dict, samples: list, outage: dict) -> None:
    import csv
    path = RAW / "network_metrics.csv"
    new = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["mode", "profile", "seed", "t", "ok", "rtt_ms", "bytes", "in_outage"])
        for s in samples:
            inout = bool(outage["start"] and outage["start"] <= s["t"] <= (outage["end"] or outage["start"]))
            w.writerow([trial["mode"], "outage", trial["seed"], f"{s['t']:.3f}",
                        int(s["ok"]), s["rtt_ms"], s["bytes"], int(inout)])


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

    env = {"experiment": "T2", "title": "Urgent delivery under a sustained WAN outage",
           "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "git_commit": _git_commit(), "host": os.uname().nodename if hasattr(os, "uname") else "?",
           "pack": T2_PACK, "obstacle_zone": OBSTACLE_ZONE,
           "tc_available": bool(shutil.which("tc")), "ping_available": bool(shutil.which("ping")),
           "uses_deployed_endpoint": False,
           "why_local_cloud": ("T2 cannot use the deployed AWS endpoint (unlike T1/T8). T2 is fundamentally a "
                    "NETWORK-DISRUPTION SIMULATION -- its purpose is to cut and restore the cloud<->edge link on a "
                    "controlled schedule, which cannot be applied to a managed AWS endpoint from this host. The "
                    "experiment IS a precisely-timed WAN cut: the outage is created by SIGSTOP-ing the cloud PROCESS the instant "
                    "the lease is ingested and SIGCONT-ing it at mission end. A managed service (API Gateway + "
                    "Fargate) cannot be paused, and its cloud<->edge link cannot be severed at a chosen instant. "
                    "T2 also needs a short lease (compressed-timing pack) so C2's offline stall reaches a "
                    "terminal state in seconds, and must drive the real supervisor in-process against a cloud "
                    "whose process it controls. So T2 owns a LOCAL cloud; T1/T8 (no outage, no compression) use "
                    "the deployed C1/C2 endpoints."),
           "note": ("Edge = the real supervisor (C1 SLM / C2 relay / C3 rules) driven in-process. "
                    "Outage = the cloud<->edge link cut by pausing the local cloud right after the lease "
                    "is granted, held to end of mission. A persistent corridor obstacle forces a tactical "
                    "decision. Compressed-timing pack (1 min urgent deadline + 8 s grace) so C2's lease "
                    "expires within a short demo window; the mechanism is identical at production scale. "
                    "RTT/loss/throughput/recovery are measured from HTTP probes and the traffic generator, "
                    "never inferred from requested settings. Real tc/netem is applied only where tc exists."),
           "trials": len(trials)}
    (OUT_DIR / "env.json").write_text(json.dumps(env, indent=1, default=str))

    _plots(trials)
    _readme(trials, env)
    print(f"[T2] wrote {OUT_DIR.relative_to(ROOT)} ({len(trials)} trials)")
    return 0


def _plots(trials: list) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as e:
        print(f"[T2] matplotlib unavailable, skipping plots: {e}", file=sys.stderr)
        return
    (OUT_DIR / "plots").mkdir(parents=True, exist_ok=True)
    colors = {"C1": "#1f77b4", "C2": "#d62728", "C3": "#7f7f7f"}
    modes = sorted({t["mode"] for t in trials})

    # outcome bar: completion time for delivered, time-to-park for parked (hatched)
    fig, ax = plt.subplots()
    for i, m in enumerate(modes):
        ts = [t for t in trials if t["mode"] == m]
        if not ts:
            continue
        t = ts[0]
        if t.get("success"):
            ax.bar(i, t.get("completion_time_s") or 0, color=colors.get(m, None),
                   label="delivered" if i == 0 else None)
        else:
            ax.bar(i, t.get("time_to_fail_s") or 0, color=colors.get(m, None), hatch="//",
                   edgecolor="black", label="failed (halted/parked)" if not any(
                       x.get("failed") for x in trials[:i]) else None)
    ax.set_xticks(range(len(modes)))
    ax.set_xticklabels(modes)
    ax.set_ylabel("time to outcome (s)")
    ax.set_title("T2: Urgent delivery under sustained WAN outage")
    ax.legend()
    for ext in ("png", "svg"):
        fig.savefig(OUT_DIR / "plots" / f"outcome.{ext}", bbox_inches="tight")
    plt.close(fig)

    # network: RTT over time with the outage window shaded, per mode
    try:
        import csv as _csv
        rows = list(_csv.DictReader((RAW / "network_metrics.csv").open()))
        fig, ax = plt.subplots()
        for m in modes:
            pts = [(float(r["t"]), float(r["rtt_ms"]), int(r["ok"]), int(r["in_outage"]))
                   for r in rows if r["mode"] == m]
            if not pts:
                continue
            t0 = pts[0][0]
            ax.plot([p[0] - t0 for p in pts], [p[1] if p[2] else 0 for p in pts],
                    label=m, color=colors.get(m, None), linewidth=0.9)
        ax.set_xlabel("time since trial start (s)")
        ax.set_ylabel("probe RTT (ms), 0 = failed/outage")
        ax.set_title("T2: measured cloud<->edge link (outage = RTT drops to 0)")
        ax.legend()
        for ext in ("png", "svg"):
            fig.savefig(OUT_DIR / "plots" / f"network_rtt.{ext}", bbox_inches="tight")
        plt.close(fig)
    except Exception as e:
        print(f"[T2] network plot skipped: {e}", file=sys.stderr)


def _readme(trials: list, env: dict) -> None:
    lines = ["# T2: Urgent delivery under a sustained WAN outage", "",
             f"- Trials: {len(trials)}  |  generated {env['generated_utc']}  |  commit {env['git_commit']}",
             f"- Pack: `{env['pack']}` (compressed timing)  |  obstacle: `{env['obstacle_zone']}`",
             f"- tc available: {env['tc_available']}  |  ping available: {env['ping_available']}", "",
             "## Scenario", "",
             "- Mission: `" + REQUEST_TEXT + "` through the real request -> allocation -> checker -> lease -> edge path.",
             "- Cloud: local, launched per `NIMBUS_MODE` (C2 is a real C2 cloud; relay never hits a C1 endpoint).",
             "- Edge: the real `edge/supervisor/supervisor.py` driven in-process (C1 edge SLM / C2 cloud relay / C3 local rules).",
             "- Outage: cloud<->edge link cut (local cloud paused) the instant the lease is ingested, held to end of mission.",
             "- A persistent corridor obstacle forces a tactical decision mid-route while the WAN is down.", "",
             "## Why T2 uses a local cloud, not the deployed AWS endpoint", "",
             "T1 and T8 point C1/C2 at the deployed clouds (and requests show in the operator GUI). **T2 cannot**, "
             "because T2 is fundamentally a **network-disruption simulation** -- its purpose is to cut and restore "
             "the cloud<->edge link on a controlled schedule, which cannot be done to a managed AWS endpoint from "
             "this host. Concretely its mechanism is a precisely-timed WAN cut:", "",
             "1. The outage is created by **`SIGSTOP`-ing the cloud process** the instant the lease is ingested and "
             "`SIGCONT`-ing it at mission end. A managed service (API Gateway v2 + Fargate) cannot be paused, and "
             "its cloud<->edge link cannot be severed at a chosen millisecond and held. Owning the cloud process "
             "locally is the only way to cut it deterministically.",
             "2. The C1-vs-C2 contrast needs a **short lease** so C2's offline stall reaches a terminal state in "
             "~15 s rather than ~15 min. T2 uses a compressed-timing pack (`packs/hospital_sim6_t2`: 1 min urgent "
             "deadline + 8 s grace); the deployed cloud is fixed to the production pack, which must not be mutated.",
             "3. T2 drives the **real edge supervisor in-process** to inject the obstacle and read the "
             "`EXPIRED_PARKED`/`ESCALATED` outcome directly; that supervisor must target a cloud whose process this "
             "script controls.",
             "4. Running it against the deployed cloud would also push dozens of throwaway urgent tasks and a forced "
             "outage onto the shared endpoint that T1/T8 depend on.", "",
             "The local cloud still runs the **real** orchestrator, allocator, rule checker, lease repo and "
             "`/relay/decide` in the requested `NIMBUS_MODE` -- only its host and timing differ from the deployment.", "",
             "## Expected contrast", "",
             "- **C1**: edge SLM detours locally, no cloud needed -> **delivered**.",
             "- **C2**: cloud-only relay can't reach the cloud to decide -> a thin relay has no local brain, so it "
             "**halts and escalates to an operator**; with the WAN down the escalation is stuck in the offline "
             "outbox (and the lease is never renewed) -> urgent delivery **fails**.",
             "- **C3**: local finite-state rules take a map-valid detour -> **delivered** (classical baseline copes "
             "with this particular event).", "",
             env["note"], "", "## Plots", "",
             "![outcome](plots/outcome.png)",
             "![network rtt](plots/network_rtt.png)", "",
             "## Results (per trial)", "",
             "| mode | outcome | completion (s) | time-to-fail (s) | deadline | rtt (ms) | loss% | recovery (s) | llm |",
             "|---|---|---|---|---|---|---|---|---|"]
    for t in trials:
        lines.append("| {mode} | {out} | {comp} | {fail} | {dl} | {rtt} | {loss} | {rec} | {llm} |".format(
            mode=t["mode"], out=t.get("outcome"), comp=t.get("completion_time_s"),
            fail=t.get("time_to_fail_s"),
            dl="met" if t.get("deadline_met") else ("MISS" if t.get("success") else "FAIL"),
            rtt=t.get("rtt_ms_mean"), loss=t.get("loss_pct"), rec=t.get("recovery_time_s"),
            llm=t.get("cloud_llm_calls")))
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
    ap.add_argument("--profile", choices=["outage"], default="outage",
                    help="T2 is outage-only by design; healthy has no config contrast.")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--iface", default=os.environ.get("T2_IFACE"), help="apply real netem on this interface if tc exists")
    ap.add_argument("--plot-only", action="store_true", help="aggregate existing raw/ into summary + plots")
    ap.add_argument("--reset", action="store_true", help="clear raw/ before this trial")
    a = ap.parse_args()

    if a.plot_only:
        return aggregate_and_plot()

    if a.reset and RAW.exists():
        for p in RAW.glob("*"):
            p.unlink()

    trial = run_trial(a.mode, a.seed, a.iface)
    RAW.mkdir(parents=True, exist_ok=True)
    with (RAW / "trials.jsonl").open("a") as f:
        f.write(json.dumps(trial, default=str) + "\n")
    print(f"[T2] {a.mode}/outage seed={a.seed}: outcome={trial.get('outcome')} "
          f"req->lease={trial.get('request_to_lease_s')}s "
          f"completion={trial.get('completion_time_s')}s time_to_fail={trial.get('time_to_fail_s')}s "
          f"deadline_met={trial.get('deadline_met')} loss%={trial.get('loss_pct')}")
    return 0 if trial.get("final_status") else 2


if __name__ == "__main__":
    raise SystemExit(main())
