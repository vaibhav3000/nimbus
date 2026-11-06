"""Shared harness for the P3 (cloud) experiments C1-C9.

Every experiment talks to the cloud over its public HTTP API, so the same code runs against
  * the deployed cloud (real Amazon Bedrock):   --target https://<api-id>.execute-api.ap-south-1.amazonaws.com
  * a local server this harness starts itself:  --target local            (rule-based stand-in model)
                                                 --target local-bedrock    (real Bedrock; needs AWS credentials)
and writes one results folder per experiment (see ResultDir):
    results/final/<ID>/raw/*.jsonl   one line per trial, exactly as measured
    results/final/<ID>/summary.csv   one row per condition, with a `source` column
    results/final/<ID>/plots/*.png   300 dpi, plus .svg
    results/final/<ID>/env.json      target, model, git commit, host, time
    results/final/<ID>/README.md     claim, PRD pass bar, command, commit, PASS/FAIL with the number

`source` says where a number came from: real_bedrock (deployed cloud or local-bedrock), mock_llm (local stand-in model),
measured (no model involved). Nothing here is modelled or hard-coded: every number is computed from the run's raw records.
"""

from __future__ import annotations

import csv
import json
import math
import os
import platform
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib import request as urlreq
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "results" / "final"
DEFAULT_DEPLOYED = "https://qrgdidp9te.execute-api.ap-south-1.amazonaws.com"
CLUSTERS = ("east-wing", "west-wing")

# ----------------------------------------------------------------------------------------------- HTTP


class Api:
    """Tiny JSON client. Every call returns (status, body, seconds) so callers can time it."""

    def __init__(self, base: str, token: str | None = None, timeout: float = 60.0):
        self.base, self.token, self.timeout = base.rstrip("/"), token, timeout

    def call(self, method: str, path: str, body: dict | None = None, *, timeout: float | None = None,
             headers: dict | None = None) -> tuple[int, dict, float]:
        data = None if body is None else json.dumps(body).encode()
        req = urlreq.Request(self.base + path, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        t0 = time.perf_counter()
        try:
            with urlreq.urlopen(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read().decode("utf-8")
                status = resp.status
        except HTTPError as err:
            status, raw = err.code, err.read().decode("utf-8", "replace")
        except (URLError, OSError, TimeoutError) as err:
            return 0, {"error": f"{type(err).__name__}: {err}"}, time.perf_counter() - t0
        dt = time.perf_counter() - t0
        try:
            out = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            out = {"error": "non-JSON reply", "raw": raw[:300]}
        return status, out if isinstance(out, dict) else {"value": out}, dt

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.call("POST", path, body or {}, **kw)


# ----------------------------------------------------------------------------------------------- targets


@dataclass
class Target:
    """Where the cloud runs. `source` labels every number measured against it."""
    name: str
    api: Api
    source: str
    mode: str = "C1"
    proc: subprocess.Popen | None = None
    data_dir: Path | None = None
    created: list = field(default_factory=list)      # task ids this run created (cancelled on close if deployed)
    is_deployed: bool = False

    def info(self) -> dict:
        st, fleet, _ = self.api.get("/fleet")
        st2, llm, _ = self.api.get("/llm-status")
        return {"target": self.name, "base_url": self.api.base, "source": self.source, "mode": fleet.get("mode", self.mode),
                "model": fleet.get("llm"), "pack": fleet.get("pack"), "llm_status": llm.get("status") if st2 == 200 else None}

    def request(self, text: str, **extra) -> tuple[int, dict, float]:
        st, out, dt = self.api.post("/requests", {"text": text, "message_id": f"exp-{uuid.uuid4().hex[:12]}", **extra})
        if out.get("task_id"):
            self.created.append(out["task_id"])
        return st, out, dt

    def close(self) -> None:
        if self.is_deployed:
            cleanup_deployed(self)
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.data_dir is not None and not os.environ.get("NIMBUS_KEEP_DATA"):
            shutil.rmtree(self.data_dir, ignore_errors=True)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_local(mode: str = "C1", *, bedrock: bool = False, extra_env: dict | None = None, tick_s: float = 1.0) -> Target:
    """Start server.py on a free port with its own empty data folder (hermetic: nothing is shared between runs)."""
    port, data = _free_port(), Path(tempfile.mkdtemp(prefix=f"nimbus-{mode}-"))
    env = {**os.environ, "PORT": str(port), "DATA_DIR": str(data), "NIMBUS_MODE": mode, "NIMBUS_PACK": "hospital_sim6",
           "NIMBUS_BACKEND": "local", "NIMBUS_DEMO_FAULTS": "1", "NIMBUS_TICK_S": str(tick_s), "PYTHONUNBUFFERED": "1"}
    env.pop("NIMBUS_EDGE_TOKEN", None)
    if bedrock:
        env.update({"NIMBUS_LLM": "bedrock", "NIMBUS_BEDROCK_MODEL": env.get("NIMBUS_BEDROCK_MODEL", "apac.amazon.nova-lite-v1:0")})
    else:
        env["NIMBUS_LLM"] = "fake"
    env.update(extra_env or {})
    log = open(data / "server.log", "w")
    proc = subprocess.Popen([sys.executable, str(ROOT / "server.py")], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    api = Api(f"http://127.0.0.1:{port}")
    for _ in range(150):
        if proc.poll() is not None:
            raise RuntimeError(f"local server exited: {(data / 'server.log').read_text()[-2000:]}")
        st, _, _ = api.get("/fleet", timeout=2)
        if st == 200:
            break
        time.sleep(0.2)
    else:
        proc.kill()
        raise RuntimeError("local server did not start")
    return Target(f"local-{mode}" + ("-bedrock" if bedrock else ""), api, "real_bedrock" if bedrock else "mock_llm",
                  mode, proc, data)


def open_target(spec: str, mode: str = "C1", **kw) -> Target:
    if spec in ("local", "local-bedrock"):
        return start_local(mode, bedrock=spec == "local-bedrock", **kw)
    url = DEFAULT_DEPLOYED if spec == "deployed" else spec
    t = Target("deployed", Api(url, token=os.environ.get("NIMBUS_ADMIN_TOKEN")), "real_bedrock", is_deployed=True)
    t.mode = t.info().get("mode") or "C1"
    return t


def cleanup_deployed(target: Target) -> None:
    """Experiments must not leave work on the shared deployment: cancel every task this run created that is still open
    (in parallel: queued tasks would otherwise be leased to real robots if someone connects edges meanwhile)."""
    from concurrent.futures import ThreadPoolExecutor

    def one(tid):
        st, t, _ = target.api.get(f"/tasks/{tid}")
        if st == 200 and t.get("status") not in ("completed", "cancelled", "failed", "rejected"):
            target.api.post(f"/tasks/{tid}/control", {"action": "cancel", "message_id": f"exp-cancel-{uuid.uuid4().hex[:10]}"})

    with ThreadPoolExecutor(16) as ex:
        list(ex.map(one, dict.fromkeys(target.created)))


def edges_connected(target: Target, max_age_s: float = 30.0) -> list[str]:
    """Clusters whose (real) edges checked in recently: experiments that issue leases must not run then."""
    st, fleet, _ = target.api.get("/fleet")
    return [c["cluster_id"] for c in fleet.get("fleet", []) if c.get("age_s") is not None and c["age_s"] <= max_age_s]


# ----------------------------------------------------------------------------------------------- simulated edges


class Edges:
    """Heartbeating edge clusters that act only on leases of tasks in `self.tasks`.

    on_lease(edge, cluster, lease) decides what each lease gets: "complete" (OUTPUT_READY after `work_s`), an
    UNEXPECTED_EVENT payload dict, or None (check in and hold). Lease arrival times are recorded per (task, version),
    measured at the poll that first saw the lease (poll period `poll_s`, so arrival time has +poll_s resolution).
    """

    def __init__(self, target: Target, *, poll_s: float = 0.25, heartbeat_s: float = 2.0, work_s: float = 0.0,
                 on_lease=None):
        self.t, self.poll_s, self.heartbeat_s, self.work_s = target, poll_s, heartbeat_s, work_s
        self.on_lease = on_lease or (lambda edge, cluster, lease: "complete")
        self.tasks: set[str] = set()
        self.seen: dict[tuple[str, int], float] = {}      # (task_id, version) -> wall time first seen
        self.leases: dict[tuple[str, int], dict] = {}
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        st, fleet, _ = target.api.get("/fleet")
        self.robots = {c["cluster_id"]: [{"id": r["id"], "state": "idle", "zone": r["zone"]} for r in c["robots"]]
                       for c in fleet.get("fleet", [])}

    @staticmethod
    def task_of(lease: dict) -> str:
        lid = lease["lease_id"]
        return lid[len("lease-"):lid.rindex("-v")]

    def start(self, poll: bool = True) -> "Edges":
        """poll=False: heartbeat only (keeps clusters healthy without reading the outbox)."""
        self.beat()
        for fn in ((self._heartbeat_loop, self._poll_loop) if poll else (self._heartbeat_loop,)):
            th = threading.Thread(target=fn, daemon=True)
            th.start()
            self._threads.append(th)
        return self

    def stop(self) -> None:
        self._stop.set()
        for th in self._threads:
            th.join(5)

    def beat(self) -> None:
        for cid, robots in self.robots.items():
            self.t.api.post("/fleet/heartbeat", {"cluster_id": cid, "robots": robots}, timeout=10)

    def _heartbeat_loop(self):
        while not self._stop.wait(self.heartbeat_s):
            try:
                self.beat()
            except Exception:
                pass

    def escalate(self, kind: str, lease: dict, payload: dict) -> tuple[int, dict, float]:
        return self.t.api.post("/escalations", {"type": kind, "message_id": f"exp-{kind.lower()}-{uuid.uuid4().hex[:10]}",
                                                "lease_version": lease["version"], "task_id": self.task_of(lease),
                                                "payload": payload}, timeout=30)

    def checkin(self, lease: dict):
        return self.t.api.post("/checkins", {"cluster_id": lease["cluster_id"], "lease_id": lease["lease_id"],
                                             "lease_version": lease["version"], "message_id": f"exp-ci-{uuid.uuid4().hex[:10]}",
                                             "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, timeout=10)

    def _poll_loop(self):
        while not self._stop.wait(self.poll_s):
            try:
                st, out, _ = self.t.api.get("/outbox?active=1", timeout=10)
            except Exception:
                continue
            if st != 200:
                continue
            now = time.time()
            for row in out.get("outbox", []):
                topic, lease = row.get("topic", ""), row.get("payload")
                if not topic.endswith("/lease") or not isinstance(lease, dict) or "lease_id" not in lease:
                    continue
                tid = self.task_of(lease)
                key = (tid, int(lease.get("version", 1)))
                if tid not in self.tasks or key in self.seen:
                    continue
                self.seen[key], self.leases[key] = now, lease
                if [s.get("cmd") for s in lease.get("task_sequence", [])] == ["stop"]:
                    continue
                threading.Thread(target=self._handle, args=(topic.split("/")[1], lease), daemon=True).start()

    def _handle(self, cluster: str, lease: dict):
        self.checkin(lease)
        what = self.on_lease(self, cluster, lease)
        if what == "complete":
            if self.work_s:
                time.sleep(self.work_s)
            self.escalate("OUTPUT_READY", lease, {"result_ref": f"s3://nimbus-outputs/{self.task_of(lease)}/proof.jpg"})
        elif isinstance(what, dict):
            self.escalate("UNEXPECTED_EVENT", lease, what)


# ----------------------------------------------------------------------------------------------- statistics


def percentile(xs, q: float):
    xs = sorted(x for x in xs if x is not None and not (isinstance(x, float) and math.isnan(x)))
    if not xs:
        return None
    k = (len(xs) - 1) * q / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    return xs[lo] if lo == hi else xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def bootstrap_ci(xs, stat=lambda v: percentile(v, 50), n: int = 2000, seed: int = 7, alpha: float = 0.05):
    xs = [x for x in xs if x is not None]
    if len(xs) < 2:
        return (None, None)
    rng = random.Random(seed)
    vals = sorted(stat([rng.choice(xs) for _ in xs]) for _ in range(n))
    return vals[int(n * alpha / 2)], vals[int(n * (1 - alpha / 2)) - 1]


def wilson(k: int, n: int, z: float = 1.96):
    if n == 0:
        return (None, None)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def r(x, nd=3):
    return None if x is None else round(x, nd)


# ----------------------------------------------------------------------------------------------- results


def git_commit() -> str:
    try:
        sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True).strip()
        dirty = subprocess.call(["git", "diff", "--quiet"], cwd=ROOT) != 0
        return sha + ("-dirty" if dirty else "")
    except Exception:
        return "unknown"


class ResultDir:
    def __init__(self, exp_id: str, title: str, target: Target | None, argv: list[str] | None = None):
        self.id, self.title = exp_id, title
        self.dir = RESULTS / exp_id
        (self.dir / "raw").mkdir(parents=True, exist_ok=True)
        (self.dir / "plots").mkdir(parents=True, exist_ok=True)
        self.command = "python " + " ".join([str(Path(sys.argv[0]).relative_to(ROOT)) if Path(sys.argv[0]).is_absolute() and ROOT in Path(sys.argv[0]).parents else sys.argv[0]] + (argv if argv is not None else sys.argv[1:]))
        self.env = {"experiment": exp_id, "title": title, "git_commit": git_commit(),
                    "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "host": platform.node(),
                    "python": platform.python_version(), "platform": platform.platform(), "command": self.command}
        if target is not None:
            self.env.update(target.info())
        self._raw: dict[str, Path] = {}

    def raw(self, name: str, row: dict) -> None:
        p = self._raw.setdefault(name, self.dir / "raw" / f"{name}.jsonl")
        with p.open("a") as f:
            f.write(json.dumps(row, default=str) + "\n")

    def reset_raw(self) -> None:
        for p in (self.dir / "raw").glob("*.jsonl"):
            p.unlink()

    def summary(self, rows: list[dict], name: str = "summary.csv") -> None:
        keys = list(dict.fromkeys(k for row in rows for k in row))
        with (self.dir / name).open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(rows)

    def json(self, name: str, obj) -> None:
        (self.dir / name).write_text(json.dumps(obj, indent=1, default=str))

    def finish(self, *, claim: str, pass_bar: str, verdict: str, measured: str, notes: list[str] | None = None,
               plots: list[str] | None = None) -> None:
        self.env["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.json("env.json", self.env)
        lines = [f"# {self.id}: {self.title}", "",
                 f"- **Claim:** {claim}",
                 f"- **PRD pass bar:** {pass_bar}",
                 f"- **Measured:** {measured}",
                 f"- **Verdict:** {verdict}",
                 f"- **Source of numbers:** {self.env.get('source', 'measured')} ({self.env.get('target', 'in-process')}, model {self.env.get('model', 'n/a')})",
                 f"- **Command:** `{self.command}`",
                 f"- **Commit:** {self.env['git_commit']}, run {self.env['started_utc']} to {self.env['finished_utc']}", ""]
        if plots:
            lines += ["## Plots", ""] + [f"![{p}](plots/{p}.png)" for p in plots] + [""]
        if notes:
            lines += ["## Notes", ""] + [f"- {n}" for n in notes] + [""]
        lines += ["Raw per-trial records are in `raw/`; `summary.csv` is computed from them by the same script."]
        (self.dir / "README.md").write_text("\n".join(lines) + "\n")
        print(f"\n[{self.id}] {verdict}: {measured}\n  -> {self.dir.relative_to(ROOT)}")


# ----------------------------------------------------------------------------------------------- plots

COLORS = {"C1": "#1f77b4", "C2": "#ff7f0e", "C3": "#7f7f7f", "real_bedrock": "#1f77b4", "mock_llm": "#7f7f7f",
          "urgent": "#d62728", "routine": "#1f77b4", "ours": "#1f77b4", "fifo": "#7f7f7f"}


def plt():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as pyplot
    pyplot.rcParams.update({"figure.figsize": (6.4, 4.0), "figure.dpi": 100, "savefig.dpi": 300, "font.size": 10,
                            "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True,
                            "grid.alpha": 0.3, "legend.frameon": False, "savefig.bbox": "tight"})
    return pyplot


def pass_line(ax, y: float, label: str, axis: str = "h"):
    (ax.axhline if axis == "h" else ax.axvline)(y, color="#d62728", ls="--", lw=1.2, label=label)


def save(fig, rd: ResultDir, name: str) -> str:
    fig.savefig(rd.dir / "plots" / f"{name}.png")
    fig.savefig(rd.dir / "plots" / f"{name}.svg")
    import matplotlib.pyplot as pyplot
    pyplot.close(fig)
    return name


def ecdf(ax, xs, label, color=None):
    xs = sorted(x for x in xs if x is not None)
    if xs:
        ax.step(xs, [(i + 1) / len(xs) for i in range(len(xs))], where="post", label=label, color=color)


def common_args(ap, default_target: str = "local-bedrock"):
    ap.add_argument("--target", default=default_target,
                    help="deployed | <https URL> | local (stand-in model) | local-bedrock (needs AWS credentials)")
    ap.add_argument("--seed", type=int, default=1)
    return ap
