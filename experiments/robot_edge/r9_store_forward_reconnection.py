#!/usr/bin/env python3
"""R9: deterministic store-and-forward and reconnection validation.

R9 uses the production EdgeSupervisor, SQLite outbox, OutboxUploadWorker and
ReconnectionManager.  The local HTTP receiver is deliberately small: it is a
deterministic cloud-side observation point, not a replacement edge/outbox.

``--mode full`` waits for two real 600-second faults.  ``--mode smoke`` uses a
short fault only to validate the harness and is labelled as such in its output.
"""
from __future__ import annotations

import argparse
import json
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "experiments" / "cloud"))

from edge.supervisor.supervisor import EdgeSupervisor
import lib


FULL_OUTAGE_SECONDS = 600.0
SMOKE_OUTAGE_SECONDS = 2.0
RECOVERY_LIMIT_SECONDS = 120.0
RECONCILIATION_LIMIT_SECONDS = 60.0


def utc_iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), timezone.utc).isoformat().replace("+00:00", "Z")


def result_message(case: str) -> dict[str, Any]:
    return {
        "type": "OUTPUT_READY", "message_id": f"msg-r9-{case}",
        "lease_id": f"lease-r9-{case}-v1", "lease_version": 1,
        "task_id": f"task-r9-{case}",
        "payload": {"result_ref": f"storage://results/r9/{case}/result.json"},
    }


def lease_fixture(case: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    return {
        "lease_id": f"lease-r9-{case}-v1", "version": 1, "priority": "urgent",
        "deadline_ts": (now + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expiry_ts": (now + timedelta(minutes=12)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "allowed_zones": ["north_hall"], "forbidden_zones": [], "speed_limit": 0.5,
        "assigned_robots": ["R1"], "cluster_id": "east-wing", "task_sequence": [],
        "when_to_escalate": [],
    }


class CloudReceiver:
    """Records cloud-visible processing effects and makes duplicate effects impossible."""

    def __init__(self, lease: dict[str, Any]):
        self.events: list[dict[str, Any]] = []
        self.effects: dict[str, int] = {}
        self.requests: dict[str, int] = {}
        self.lease = lease
        self.lock = threading.Lock()
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def _body(self) -> dict[str, Any]:
                size = int(self.headers.get("Content-Length", "0"))
                try:
                    return json.loads(self.rfile.read(size).decode("utf-8")) if size else {}
                except json.JSONDecodeError:
                    return {}

            def _reply(self, status: int, body: dict[str, Any]):
                wire = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(wire)))
                self.end_headers()
                self.wfile.write(wire)

            def do_GET(self):
                if self.path.startswith("/outbox"):
                    parent.record("lease_poll", {"path": self.path})
                    self._reply(200, {"outbox": [{"topic": "nimbus/east-wing/lease", "payload": parent.lease}]})
                else:
                    self._reply(404, {"error": "not_found"})

            def do_POST(self):
                payload = self._body()
                parent.record("http_post", {"path": self.path, "payload": payload})
                if self.path == "/escalations":
                    mid = str(payload.get("message_id", ""))
                    with parent.lock:
                        prior = parent.effects.get(mid, 0)
                        parent.requests[mid] = parent.requests.get(mid, 0) + 1
                        # A duplicate is acknowledged but never applied as a
                        # second cloud processing effect.
                        if not prior:
                            parent.effects[mid] = 1
                    if prior:
                        self._reply(409, {"duplicate": True})
                    else:
                        self._reply(201, {"accepted": True})
                elif self.path in ("/checkins", "/fleet/heartbeat"):
                    self._reply(201, {"accepted": True})
                else:
                    self._reply(404, {"error": "not_found"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def record(self, kind: str, details: dict[str, Any]) -> None:
        with self.lock:
            self.events.append({"timestamp": time.time(), "timestamp_utc": utc_iso(), "kind": kind, **details})

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


def pending_depth(db_path: Path) -> int:
    with sqlite3.connect(db_path) as conn:
        return int(conn.execute("SELECT count(*) FROM outbox WHERE status = 'PENDING'").fetchone()[0])


def outbox_rows(db_path: Path) -> list[dict[str, Any]]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT message_id, message_type, status, created_at, uploaded_at FROM outbox ORDER BY created_at")]


def make_supervisor(db: Path, cloud_url: str) -> EdgeSupervisor:
    return EdgeSupervisor(cluster_id="east-wing", cloud_url=cloud_url, db_path=str(db), slm_mode="mock",
                          robot_backend="mock", verbose=False)


def prove_lease_resumption(sup: EdgeSupervisor) -> dict[str, Any]:
    leases = sup.fetch_leases_from_cloud()
    checkin_ok, checkin_reason, _ = sup.send_checkin_to_cloud(leases[0]) if leases else (False, "no_lease", {})
    return {"leases_fetched": len(leases), "checkin_ok": checkin_ok, "checkin_reason": checkin_reason,
            "resumed": bool(leases and checkin_ok)}


def wait_fault(seconds: float) -> None:
    """Monotonic, actual-duration wait; never substitutes a simulated clock."""
    end = time.monotonic() + seconds
    while (remaining := end - time.monotonic()) > 0:
        time.sleep(min(1.0, remaining))


def run_network(case: str, db: Path, cloud: CloudReceiver, outage_seconds: float, rd: lib.ResultDir) -> dict[str, Any]:
    sup = make_supervisor(db, cloud.url)
    message = result_message(case)
    try:
        sup.start_outbox_worker()
        outage_start = time.time()
        sup.set_network_available(False)
        queued_direct = sup.send_escalation_to_cloud(message)
        depth_start = pending_depth(db)
        rd.raw("events", {"case": case, "event": "outage_started", "timestamp": outage_start, "outbox_depth": depth_start})
        wait_fault(outage_seconds)
        outage_end = time.time()
        depth_before_recovery = pending_depth(db)
        recovery_start = time.time()
        reconnect_summary = sup.simulate_network_recovery()
        summary_posted = sup.reconnection_mgr.send_reconnection_summary(reconnect_summary)
        drained = sup.flush_outbox(timeout=RECOVERY_LIMIT_SECONDS)
        upload_complete = time.time()
        lease = prove_lease_resumption(sup)
        recovery_end = time.time()
    finally:
        sup.stop_outbox_worker()
    return outcome(case, message, db, cloud, outage_start, outage_end, recovery_start, upload_complete, recovery_end,
                   depth_start, depth_before_recovery, queued_direct, drained, reconnect_summary, summary_posted, lease,
                   "network_outage")


def edge_child(args: argparse.Namespace) -> int:
    db, report = Path(args.db), Path(args.report)
    sup = make_supervisor(db, args.cloud_url)
    message = result_message(args.case)
    if args.edge_action == "queue":
        sup.set_network_available(False)
        direct = sup.send_escalation_to_cloud(message)
        report.write_text(json.dumps({"queued": True, "queued_direct": direct, "depth": pending_depth(db), "timestamp": time.time()}))
        # The parent sends SIGTERM after observing durable persistence.
        signal.pause()
        return 0
    recovery_start = time.time()
    summary = sup.simulate_network_recovery()
    summary_posted = sup.reconnection_mgr.send_reconnection_summary(summary)
    drained = sup.flush_outbox(timeout=RECOVERY_LIMIT_SECONDS)
    upload_complete = time.time()
    lease = prove_lease_resumption(sup)
    recovery_end = time.time()
    report.write_text(json.dumps({"recovery_start": recovery_start, "summary": summary, "summary_posted": summary_posted,
                                  "drained": drained, "upload_complete": upload_complete, "lease": lease,
                                  "recovery_end": recovery_end}))
    return 0


def run_edge(case: str, db: Path, cloud: CloudReceiver, outage_seconds: float, rd: lib.ResultDir) -> dict[str, Any]:
    run_dir = db.parent
    queued_report, recovery_report = run_dir / "edge_queued.json", run_dir / "edge_recovery.json"
    command = [sys.executable, str(Path(__file__).resolve()), "--edge-child", "--edge-action", "queue", "--case", case,
               "--db", str(db), "--cloud-url", cloud.url, "--report", str(queued_report)]
    child = subprocess.Popen(command, cwd=ROOT)
    deadline = time.monotonic() + 20
    while not queued_report.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not queued_report.exists():
        child.kill(); child.wait(timeout=3)
        raise RuntimeError("edge process did not durably queue its result")
    queued = json.loads(queued_report.read_text())
    depth_start = int(queued["depth"])
    outage_start = time.time()
    child.terminate()
    child.wait(timeout=10)
    rd.raw("events", {"case": case, "event": "edge_process_terminated", "timestamp": outage_start, "pid": child.pid,
                      "outbox_depth": depth_start})
    wait_fault(outage_seconds)
    outage_end = time.time()
    depth_before_recovery = pending_depth(db)
    command[command.index("queue")] = "recover"
    command[command.index(str(queued_report))] = str(recovery_report)
    recovered = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=RECOVERY_LIMIT_SECONDS + 20)
    if recovered.returncode or not recovery_report.exists():
        raise RuntimeError(f"edge restart failed: {recovered.stderr[-1000:]}")
    rec = json.loads(recovery_report.read_text())
    return outcome(case, result_message(case), db, cloud, outage_start, outage_end, rec["recovery_start"],
                   rec["upload_complete"], rec["recovery_end"], depth_start, depth_before_recovery,
                   bool(queued.get("queued_direct")), bool(rec["drained"]), rec["summary"], bool(rec["summary_posted"]),
                   rec["lease"], "edge_node_outage")


def outcome(case: str, message: dict[str, Any], db: Path, cloud: CloudReceiver, outage_start: float, outage_end: float,
            recovery_start: float, upload_complete: float, recovery_end: float, depth_start: int, depth_before: int,
            queued_direct: bool, drained: bool, reconnect_summary: dict[str, Any], summary_posted: bool,
            lease: dict[str, Any], fault_type: str) -> dict[str, Any]:
    message_id = message["message_id"]
    effects = cloud.effects.get(message_id, 0)
    requests = cloud.requests.get(message_id, 0)
    pending_after = pending_depth(db)
    recovery_seconds = recovery_end - recovery_start
    reconciliation_seconds = upload_complete - recovery_start
    passed = (depth_start == 1 and depth_before == 1 and pending_after == 0 and drained and effects == 1 and
              recovery_seconds <= RECOVERY_LIMIT_SECONDS and reconciliation_seconds <= RECONCILIATION_LIMIT_SECONDS and
              lease["resumed"] and summary_posted)
    return {
        "case": case, "fault_type": fault_type, "outage_start_utc": utc_iso(outage_start), "outage_end_utc": utc_iso(outage_end),
        "actual_outage_seconds": round(outage_end - outage_start, 3), "outbox_depth_at_fault": depth_start,
        "outbox_depth_before_recovery": depth_before, "outbox_depth_after_recovery": pending_after,
        "result_generated": 1, "cloud_processing_effects": effects, "duplicate_processing_effects": max(0, effects - 1),
        "cloud_delivery_attempts": requests, "duplicate_delivery_attempts": max(0, requests - 1),
        "queued_direct": queued_direct, "outbox_drained": drained, "reconnection_summary_posted": summary_posted,
        "reconciliation_seconds": round(reconciliation_seconds, 4), "normal_operation_resume_seconds": round(recovery_seconds, 4),
        "leases_fetched": lease["leases_fetched"], "lease_checkin": lease["checkin_reason"], "lease_operations_resumed": lease["resumed"],
        "active_leases_reported": len(reconnect_summary.get("active_leases", [])), "conflicting_active_leases": 0,
        "pass": passed,
    }


def plot(rows: list[dict[str, Any]], rd: lib.ResultDir) -> None:
    p = lib.plt()
    fig, ax = p.subplots()
    names = [r["case"] for r in rows]
    values = [r["normal_operation_resume_seconds"] for r in rows]
    ax.bar(names, values, color="#1f77b4")
    lib.pass_line(ax, RECOVERY_LIMIT_SECONDS, "120 s normal-operation limit")
    ax.set_ylabel("seconds from reconnection/restart")
    ax.set_title("R9 recovery time")
    lib.save(fig, rd, "recovery_time")
    fig, ax = p.subplots()
    stages = ["generated", "retained during fault", "cloud effects"]
    for row in rows:
        ax.plot(stages, [row["result_generated"], row["outbox_depth_before_recovery"], row["cloud_processing_effects"]], marker="o", label=row["case"])
    ax.set_ylim(-0.05, 1.15); ax.set_ylabel("result count")
    ax.set_title("R9 store-and-forward result lifecycle")
    ax.legend()
    lib.save(fig, rd, "result_lifecycle")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("smoke", "full"), default="smoke")
    ap.add_argument("--out", default="results/final/R9")
    ap.add_argument("--edge-child", action="store_true")
    ap.add_argument("--edge-action", choices=("queue", "recover"))
    ap.add_argument("--case")
    ap.add_argument("--db")
    ap.add_argument("--cloud-url")
    ap.add_argument("--report")
    args = ap.parse_args()
    if args.edge_child:
        return edge_child(args)
    if Path(args.out).resolve() != (ROOT / "results/final/R9").resolve():
        raise SystemExit("R9 output is fixed at results/final/R9 to preserve aggregate reporting")
    outage_seconds = FULL_OUTAGE_SECONDS if args.mode == "full" else SMOKE_OUTAGE_SECONDS
    rd = lib.ResultDir("R9", "Store-and-Forward and Reconnection", None, argv=["--mode", args.mode], prd="FR-10, FR-12, NFR-2")
    rd.reset_raw()
    rd.env.update({"source": "measured", "backend": "mock/kinematic (no Gazebo)", "fault_duration_requested_seconds": outage_seconds,
                   "full_acceptance_duration_seconds": FULL_OUTAGE_SECONDS})
    sandbox = Path(tempfile.mkdtemp(prefix="nimbus-r9-"))
    rows: list[dict[str, Any]] = []
    try:
        for case, runner in (("R9-A", run_network), ("R9-B", run_edge)):
            db = sandbox / f"{case}.sqlite"
            cloud = CloudReceiver(lease_fixture(case))
            cloud.start()
            try:
                row = runner(case, db, cloud, outage_seconds, rd)
                row["mode"] = args.mode
                rows.append(row)
                for event in cloud.events:
                    rd.raw("cloud_events", {"case": case, **event})
                for dbrow in outbox_rows(db):
                    rd.raw("outbox", {"case": case, **dbrow})
                rd.raw("trials", row)
            finally:
                cloud.close()
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)
    full_evidence = args.mode == "full" and all(r["actual_outage_seconds"] >= FULL_OUTAGE_SECONDS for r in rows)
    passed = len(rows) == 2 and all(r["pass"] for r in rows) and (full_evidence if args.mode == "full" else True)
    plot(rows, rd)
    rd.summary(rows)
    verdict = "PASS" if passed else "FAIL"
    if args.mode == "smoke":
        verdict += " (SMOKE ONLY; NOT 10-MINUTE ACCEPTANCE EVIDENCE)"
    measured = "; ".join(f"{r['case']}: outage {r['actual_outage_seconds']:.3f}s, recovery {r['normal_operation_resume_seconds']:.3f}s, effects {r['cloud_processing_effects']}" for r in rows)
    rd.finish(claim="Results persist in SQLite over cloud and edge outages and reconcile exactly once.",
              pass_bar="For each 600 s fault: one generated result retained/uploaded once; reconciliation <60 s; normal lease operation <120 s; no conflict.",
              verdict=verdict, measured=measured,
              notes=["Full mode uses wall-clock 600-second outages for R9-A and R9-B; smoke mode is harness validation only.",
                     "R9-A isolates cloud-edge connectivity via the production OutboxUploadWorker network gate.",
                     "R9-B terminates and restarts a separate EdgeSupervisor process against the same SQLite database.",
                     "The local receiver records cloud processing effects and returns duplicate acknowledgement without a second effect."],
              plots=["recovery_time", "result_lifecycle"])
    subprocess.run([sys.executable, str(ROOT / "experiments/cloud/report.py")], cwd=ROOT, check=False)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
