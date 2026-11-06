"""C7 - Cloud model outage (E5 cloud part, FR-18).

A steady stream of requests (Poisson, mean gap --gap s) runs for --duration s while simulated edges deliver every
lease after --work s. At --outage-at s the model is made unavailable for --outage s through the cloud's own fault
switch (POST /llm/outage {"seconds": N}: every model call raises, exactly like a Bedrock throttle or timeout).
Measured per request: request-to-decision time, outcome (leased / queued / asked), whether the model answered
(trace llm events) or the rules did, the user-facing notice, and the breaker state (/llm-status) sampled each second.
Also: tasks leased BEFORE the outage that complete DURING it (leased work continues).

    python experiments/p3/c7_bedrock_outage.py --target local                  # stand-in model behind the same breaker
    NIMBUS_ADMIN_TOKEN=... python experiments/p3/c7_bedrock_outage.py --target deployed   # real Bedrock (needs the admin token)
"""

from __future__ import annotations

import argparse
import random
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

TEXTS = ["Deliver a wheelchair to 2B, routine.", "Deliver a blanket to 2C.", "Bring a monitor to ward 3.",
         "IV pole to 2C, urgent.", "Please bring a wheelchair to ward 3 within 30 minutes.", "A blanket to 2B please."]


def phase(t: float, a) -> str:
    return "before" if t < a.outage_at else "during" if t < a.outage_at + a.outage else "after"


def main(argv=None) -> int:
    ap = lib.common_args(argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter), "local")
    ap.add_argument("--duration", type=float, default=1200)
    ap.add_argument("--outage-at", type=float, default=300)
    ap.add_argument("--outage", type=float, default=600)
    ap.add_argument("--gap", type=float, default=15, help="mean seconds between requests")
    ap.add_argument("--work", type=float, default=40, help="seconds an edge takes to deliver a lease")
    a = ap.parse_args(argv)
    rng = random.Random(a.seed)
    target = lib.open_target(a.target)
    rd = lib.ResultDir("C7" if a.target != "local" else "C7_local", "Cloud model outage and rule fallback (FR-18)", target)
    rd.reset_raw()
    edges = lib.Edges(target, work_s=a.work).start()
    t_start = time.time()
    stop = threading.Event()
    status_log = []

    def sample():
        while not stop.wait(1.0):
            st, s, _ = target.api.get("/llm-status", timeout=10)
            if st == 200:
                rec = {"t": round(time.time() - t_start, 2), "fault_down": s.get("fault_down"), "circuit_open": s.get("circuit_open"),
                       "status": s.get("status")}
                status_log.append(rec)
                rd.raw("llm_status", rec)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    outage_sent = outage_reply = None
    recs = []
    try:
        next_t = 0.0
        while True:
            now = time.time() - t_start
            if now >= a.duration:
                break
            if outage_sent is None and now >= a.outage_at:
                st, rep, _ = target.api.post("/llm/outage", {"seconds": a.outage})
                outage_sent, outage_reply = now, {"http": st, **rep}
                rd.raw("events", {"t": round(now, 2), "event": "outage_started", "reply": outage_reply})
                if st not in (200, 201):
                    raise SystemExit(f"/llm/outage refused ({st}): {rep}. On the deployment set NIMBUS_ADMIN_TOKEN.")
            if now < next_t:
                time.sleep(min(0.2, next_t - now))
                continue
            next_t = now + rng.expovariate(1.0 / a.gap)
            text = rng.choice(TEXTS)
            st, out, dt = target.request(text)
            tid = out.get("task_id")
            if tid:
                edges.tasks.add(tid)
            rec = {"t": round(now, 2), "phase": phase(now, a), "text": text, "http": st, "task_id": tid,
                   "status": out.get("status"), "decision_s": round(dt, 3), "notice": out.get("notice"), "reason": out.get("reason")}
            if tid:
                s, tr, _ = target.api.get(f"/tasks/{tid}/trace", timeout=20)
                llm = [e["detail"] for e in tr.get("events", []) if e.get("kind") == "llm"]
                rec["model_calls_ok"] = sum(1 for d in llm if not d.get("error"))
                rec["model_calls_failed"] = sum(1 for d in llm if d.get("error"))
                s, tl, _ = target.api.get(f"/tasks/{tid}/timeline", timeout=20)
                rows = tl.get("timeline", [])
                prop = next((r["output"] for r in rows if r["output"].get("decision") in ("propose_lease", "queue", "no_lease", "ask_clarify")), {})
                rec["proposed_by"] = prop.get("proposed_by")
                rec["understood_by"] = "model" if rec["model_calls_ok"] else "rules"
            recs.append(rec)
            rd.raw("requests", rec)
        target.api.post("/llm/outage", {"seconds": 0})
        time.sleep(a.work + 5)      # let the last leases finish
    finally:
        stop.set()
        sampler.join(3)
        # completion of every task: when did it finish relative to the outage
        for r in recs:
            if r.get("task_id"):
                s, t, _ = target.api.get(f"/tasks/{r['task_id']}")
                r["final_status"] = t.get("status")
        edges.stop()
        target.close()
    # leased-before, completed-during: from the edges' lease times and the final statuses
    out_lo, out_hi = a.outage_at, a.outage_at + a.outage
    leased_before = [r for r in recs if r["phase"] == "before" and r["status"] == "leased"]
    during = [r for r in recs if r["phase"] == "during"]
    during_leased = [r for r in during if r["status"] == "leased"]
    during_model = [r for r in during if r.get("model_calls_ok")]
    told = [r for r in during if r.get("notice")]
    trip = next((s["t"] for s in status_log if s["t"] >= out_lo and (s["circuit_open"] or s["fault_down"])), None)
    recov = next((s["t"] for s in status_log if s["t"] >= out_hi and not s["fault_down"] and not s["circuit_open"]), None)
    summary = []
    for ph in ("before", "during", "after"):
        sub = [r for r in recs if r["phase"] == ph]
        d = [r["decision_s"] for r in sub]
        summary.append({"phase": ph, "requests": len(sub), "leased": sum(r["status"] == "leased" for r in sub),
                        "queued": sum(r["status"] == "queued" for r in sub), "asked": sum(r["status"] == "needs_clarification" for r in sub),
                        "rejected_or_error": sum(r["status"] in ("rejected", None) for r in sub),
                        "understood_by_model": sum(r.get("understood_by") == "model" for r in sub),
                        "user_notified_reduced_mode": sum(bool(r.get("notice")) for r in sub),
                        "decision_p50_s": lib.r(lib.percentile(d, 50)), "decision_p95_s": lib.r(lib.percentile(d, 95)),
                        "completed_by_end": sum(r.get("final_status") == "completed" for r in sub), "source": target.source})
    rd.summary(summary)
    plt = lib.plt()
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(8, 5), sharex=True, gridspec_kw={"height_ratios": [1, 2]})
    xs = [s["t"] / 60 for s in status_log]
    ax1.step(xs, [1 if s["circuit_open"] else 0 for s in status_log], where="post", label="breaker open", color="#d62728")
    ax1.step(xs, [0.5 if s["fault_down"] else 0 for s in status_log], where="post", label="model unavailable", color="#7f7f7f")
    ax1.set_yticks([]); ax1.legend(fontsize=8, loc="upper right"); ax1.set_title("C7: model outage, breaker and lease source")
    for kind, col, mk in (("model", "#1f77b4", "o"), ("rules", "#7f7f7f", "s")):
        sub = [r for r in recs if r.get("understood_by") == kind]
        ax2.scatter([r["t"] / 60 for r in sub], [r["decision_s"] for r in sub], color=col, marker=mk, s=18, label=f"understood by {kind}")
    bad = [r for r in recs if r["status"] not in ("leased", "queued", "needs_clarification")]
    ax2.scatter([r["t"] / 60 for r in bad], [r["decision_s"] for r in bad], color="#d62728", marker="x", label="no lease (rejected/error)")
    for ax in (ax1, ax2):
        ax.axvspan(out_lo / 60, out_hi / 60, color="#d62728", alpha=0.08)
    ax2.set_xlabel("minutes into the run (shaded: model unavailable)"); ax2.set_ylabel("request to decision (s)")
    ax2.legend(fontsize=8)
    name = lib.save(fig, rd, "outage_timeline")
    ok = (len(during) > 0 and len(during_leased) + sum(r["status"] == "queued" for r in during) == len(during) - sum(r["status"] == "needs_clarification" for r in during)
          and not during_model and len(told) == len([r for r in during if r["status"] in ("leased", "queued")])
          and all(r.get("final_status") == "completed" for r in leased_before))
    rd.finish(claim="When the cloud model is unavailable, leased work continues, new requests still get leases from the deterministic rules, and users are told.",
              pass_bar="Model calls forced to fail for 10 min mid-run: leased tasks continue; new requests still leased via the fallback; user told (FR-18).",
              verdict="PASS" if ok else "FAIL",
              measured=(f"{len(during)} requests during the {a.outage:.0f} s outage: {len(during_leased)} leased, "
                        f"{sum(r['status'] == 'queued' for r in during)} queued for a robot, {len(during_model)} answered by the model, "
                        f"{len(told)} carried the reduced-autonomy notice; {sum(r.get('final_status') == 'completed' for r in leased_before)}/"
                        f"{len(leased_before)} tasks leased before the outage completed; breaker/fault seen at {trip} s, "
                        f"normal again at {recov} s (outage {out_lo:.0f} to {out_hi:.0f} s)"),
              notes=[f"Outage started with POST /llm/outage at {outage_sent} s, reply {outage_reply}.",
                     "Requests the rules cannot understand still get a question; they are not counted as failures."],
              plots=[name])
    return 0


if __name__ == "__main__":
    sys.exit(main())
