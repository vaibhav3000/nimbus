"""C6 - Re-plan and hand-over latency (FR-16, T5).

30 unexpected events on a good network (a blocked corridor, a closed door, a robot drive fault; 10 each), each on a
live lease held by a simulated edge (experiments/p3/scenarios.py). For each: the cloud's decision time (POST
/escalations round trip, including the model call), the time until the edge sees the new lease version, which action
the cloud took, and whether the new plan avoids the closed zone or the failed robot. The model's share of the decision
time comes from the task's trace.

    python experiments/p3/c6_replan.py --target deployed      # real Bedrock (run when nothing else uses the deployment)
    python experiments/p3/c6_replan.py --target local         # stand-in model
"""

from __future__ import annotations

import argparse
import random
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402
import scenarios  # noqa: E402


def plots(rd, recs) -> list[str]:
    plt = lib.plt()
    names = []
    fig, ax = plt.subplots()
    for kind in scenarios.KINDS:
        lib.ecdf(ax, [r["decision_s"] for r in recs if r["kind"] == kind and r.get("decision_s") is not None], kind.replace("_", " "))
    lib.ecdf(ax, [r["edge_saw_new_version_s"] for r in recs if r.get("edge_saw_new_version_s") is not None],
             "edge sees new lease (all kinds)", color="black")
    lib.pass_line(ax, 30, "FR-16: new plan within 30 s", axis="v")
    ax.set_xscale("log"); ax.set_xlabel("seconds after the edge reported the event (log)"); ax.set_ylabel("share of events")
    ax.set_title("C6: time to a new plan"); ax.legend(fontsize=8)
    names.append(lib.save(fig, rd, "replan_latency_cdf"))
    fig, ax = plt.subplots(figsize=(6.4, 3.6))
    dec = [r for r in recs if r.get("decision_s") is not None]
    model = [r["model_ms"] / 1000 for r in dec]
    rest = [max(0.0, r["decision_s"] - r["model_ms"] / 1000) for r in dec]
    labels = [lib.percentile([r["decision_s"] for r in dec], q) for q in (50, 95)]
    import statistics
    ax.barh(["model (Bedrock or stand-in)", "rule checker, store, network"], [statistics.median(model), statistics.median(rest)],
            color=["#1f77b4", "#7f7f7f"])
    ax.set_xlabel("median seconds per decision")
    ax.set_title(f"C6: where the decision time goes (p50 {labels[0]:.2f} s, p95 {labels[1]:.2f} s)")
    names.append(lib.save(fig, rd, "decision_breakdown"))
    fig, ax = plt.subplots(figsize=(6.4, 3.4))
    c = Counter((r["kind"], r.get("outcome")) for r in recs)
    outcomes = sorted({o for _, o in c})
    bottom = [0] * len(scenarios.KINDS)
    for o in outcomes:
        vals = [c.get((k, o), 0) for k in scenarios.KINDS]
        ax.bar([k.replace("_", "\n") for k in scenarios.KINDS], vals, bottom=bottom, label=o)
        bottom = [b + v for b, v in zip(bottom, vals)]
    ax.set_ylabel("events"); ax.set_title("C6: what the cloud did"); ax.legend(fontsize=7)
    names.append(lib.save(fig, rd, "actions"))
    return names


def main(argv=None) -> int:
    ap = lib.common_args(argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter))
    ap.add_argument("--events", type=int, default=32)
    a = ap.parse_args(argv)
    rng = random.Random(a.seed)
    target = lib.open_target(a.target)
    rd = lib.ResultDir("C6" if a.target != "local" else "C6_local", "Re-plan and hand-over latency (FR-16)", target)
    rd.reset_raw()
    edges = lib.Edges(target, on_lease=lambda e, c, l: None).start()       # hold every lease: the incident is scripted
    recs = []
    try:
        for i in range(a.events):
            kind = scenarios.KINDS[i % len(scenarios.KINDS)]
            rec = scenarios.run_incident(target, edges, kind, rng)
            rec.update(event=i, source=target.source)
            rd.raw("events", rec)
            recs.append(rec)
            print(f"[C6] {i + 1}/{a.events} {kind}: {rec.get('outcome')} decision {rec.get('decision_s')} s")
    finally:
        edges.stop()
        target.close()
    dec = [r["decision_s"] for r in recs if r.get("decision_s") is not None]
    planned = [r for r in recs if r.get("decision_s") is not None]
    new_plan = [r for r in planned if (r.get("new_version") or 1) >= 2]
    avoid_ok = [r for r in new_plan if r.get("new_plan_avoids_zone") is not False and r.get("new_plan_avoids_failed_robot") is not False]
    p95 = lib.percentile(dec, 95)
    summary = []
    for kind in scenarios.KINDS + ("all",):
        sub = [r for r in planned if kind == "all" or r["kind"] == kind]
        d = [r["decision_s"] for r in sub]
        e = [r["edge_saw_new_version_s"] for r in sub if r.get("edge_saw_new_version_s") is not None]
        ci = lib.bootstrap_ci(d)
        summary.append({"kind": kind, "events": len(sub), "source": target.source,
                        "decision_p50_s": lib.r(lib.percentile(d, 50)), "decision_p50_ci_low": lib.r(ci[0]), "decision_p50_ci_high": lib.r(ci[1]),
                        "decision_p95_s": lib.r(lib.percentile(d, 95)), "decision_max_s": lib.r(max(d) if d else None),
                        "edge_new_version_p50_s": lib.r(lib.percentile(e, 50)), "edge_new_version_p95_s": lib.r(lib.percentile(e, 95)),
                        "new_lease_versions": sum((r.get("new_version") or 1) >= 2 for r in sub),
                        "model_ms_p50": lib.r(lib.percentile([r["model_ms"] for r in sub], 50), 0),
                        "outcomes": dict(Counter(r.get("outcome") for r in sub))})
    rd.summary(summary)
    names = plots(rd, recs)
    ok = bool(dec) and p95 is not None and p95 <= 30 and len(avoid_ok) == len(new_plan)
    rd.finish(claim="After an unexpected event the cloud issues a new plan (new lease version, hand-over, wait or operator alert) within about 30 s.",
              pass_bar="New plan issued within about 30 s on a good network (FR-16).",
              verdict="PASS" if ok else "FAIL",
              measured=(f"decision p95 {p95:.2f} s, p50 {lib.percentile(dec, 50):.2f} s over {len(dec)} events; "
                        f"{len(new_plan)} new lease versions, {len(avoid_ok)}/{len(new_plan)} avoid the closed zone or failed robot; "
                        f"outcomes {dict(Counter(r.get('outcome') for r in recs))}"),
              notes=["Decision time is the POST /escalations round trip from this machine, so it includes the network to the cloud.",
                     "On this map a delivery from the south store to 2B or 2C has only one corridor inside its wing, so blocking it "
                     "often leaves no route in the cluster: then the cloud waits, hands over, or alerts the operator. Those are plans too; "
                     "the 'actions' plot shows which.",
                     "Every event's zone is reopened and its task cancelled before the next event."],
              plots=names)
    return 0


if __name__ == "__main__":
    sys.exit(main())
