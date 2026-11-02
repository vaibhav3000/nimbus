"""E3: C1 vs C2 vs C3 on tasks T1-T8 (PRD Sec. 7), cloud side, in process.

Drives the real Orchestrator on a fixed clock with a seeded workload, so a run
is repeatable (NFR-6). For each configuration it plays the edge's part from the
lease: C1 decides locally (no cloud call), C2 asks the cloud before every
sub-task (decide_step), C3 uses the hand-written rules. It reports success,
model calls and tokens per task, a cost per task from the price table, and a
MODELLED decision latency (calls x model latency + round trips; not measured).

    python scripts/run_e3.py                      # offline stand-in model, 10 seeds
    python scripts/run_e3.py --seeds 3 --out results/e3
    NIMBUS_LLM=bedrock NIMBUS_BEDROCK_MODEL=apac.amazon.nova-lite-v1:0 \
        NIMBUS_CASSETTE=data/e3.cassette.json NIMBUS_CASSETTE_MODE=record python scripts/run_e3.py --seeds 1

Writes results.json, results.csv and two SVG plots. Tasks the system cannot run yet
are listed as "unsupported" rather than dropped.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import statistics
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cloud.clock import FixedClock  # noqa: E402
from cloud.orchestrator import Orchestrator  # noqa: E402

CONFIGS = ("C1", "C2", "C3")
# Nova Lite on-demand list prices, USD per token. Symbolic in the PRD; change with --price-in/--price-out.
PRICE_IN, PRICE_OUT = 0.06e-6, 0.24e-6
LLM_LATENCY_S, WAN_RTT_S = 2.5, 0.08     # measured 2-3 s per step on Bedrock in MS1; a good-network RTT
FLAT_PER_DECISION = 2e-7                 # Fargate/DynamoDB/IoT share per cloud decision (stand-in for the Sec. 7 terms)


class Tally:
    def __init__(self):
        self.calls = self.tin = self.tout = self.cloud_decisions = 0
        self.ok = False
        self.note = ""

    def add(self, out):
        self.calls += getattr(out, "llm_calls", 0)
        self.tin += getattr(out, "tokens_in", 0)
        self.tout += getattr(out, "tokens_out", 0)
        self.cloud_decisions += 1


def new_orch(config: str, workdir: Path, clock: FixedClock) -> Orchestrator:
    os.environ["NIMBUS_MODE"] = config
    return Orchestrator.from_env(workdir, clock=clock)


def task(n: int, text: str) -> dict:
    return {"task_id": f"task-{n:06d}", "request_text": text, "status": "received"}


def lease_it(orch, n, text, tally, answer=None):
    t = task(n, text)
    out = orch.handle_request(t, text)
    tally.add(out)
    if out.kind == "clarify" and answer:
        t.update(status="needs_clarification", question=out.question)
        out = orch.handle_request(t, text, question=out.question, answer=answer)
        tally.add(out)
    if out.kind == "leased":
        t.update(status="leased", spec=out.spec, lease=out.lease, reservation=out.reservation)
    elif out.kind == "queued":
        t.update(status="queued", spec=out.spec)
    return t, out


def relay_steps(orch, config, t, lease, tally, blocked=()):
    """The edge's non-reflex decisions for one lease. C1 and C3 decide on the edge (no cloud call)."""
    if config != "C2":
        return True
    ok = True
    for i, step in enumerate(lease["task_sequence"]):
        ev = {"step_index": i, "cmd": step["cmd"], "zone": lease["allowed_zones"][0],
              "target": lease["allowed_zones"][-1], "situation": "next step"}
        if blocked and step["cmd"] == "go_to":
            ev["blocked_zones"] = list(blocked)
        res = orch.decide_step(t["task_id"], lease["version"], lease["cluster_id"], ev)
        tally.calls += res["calls"]; tally.tin += res["tokens_in"]; tally.tout += res["tokens_out"]
        tally.cloud_decisions += 1
        ok = ok and res["decision"]["action"] != "abort"
    return ok


# -- the tasks ------------------------------------------------------------------------------------------------------------

def t1(orch, config, rng, tally):
    t, out = lease_it(orch, 1, "Deliver a wheelchair to ward 3, routine.", tally)
    tally.ok = out.kind == "leased" and relay_steps(orch, config, t, out.lease, tally)


def t2(orch, config, rng, tally):
    for i in range(rng.randint(1, 2)):     # background traffic on the same wing
        lease_it(orch, 10 + i, "Deliver a blanket to 2A, routine.", Tally())
    t, out = lease_it(orch, 1, "Need a monitor at 2A, urgent.", tally)
    tally.ok = out.kind == "leased" and out.lease["priority"] == "urgent" and relay_steps(orch, config, t, out.lease, tally)


def t3(orch, config, rng, tally):
    t, out = lease_it(orch, 1, "a pump to 2B", tally, answer="Yes, a paediatric pump please.")
    tally.ok = out.kind == "leased" and relay_steps(orch, config, t, out.lease, tally)


def t4(orch, config, rng, tally):
    t, out = lease_it(orch, 1, "Deliver a wheelchair to 2B-3, routine.", tally)
    if out.kind != "leased":
        return
    # E3 blocked: C1/C3 detour on the edge inside the lease; C2 asks the cloud at every step.
    tally.ok = relay_steps(orch, config, t, out.lease, tally, blocked=("E3",))
    detour = any(z in out.lease["allowed_zones"] for z in ("E4",))
    tally.ok = tally.ok and detour
    tally.note = "detour zone present in the lease" if detour else "no detour zone in lease"


def t6(orch, config, rng, tally):
    orch.policies.declare("2A", "isolation", "infection-control")
    routine, r_out = lease_it(orch, 1, "Deliver a monitor to 2A, routine.", tally)
    urgent, u_out = lease_it(orch, 2, "Deliver a monitor to 2A, urgent.", tally)
    tally.ok = r_out.kind == "queued" and u_out.kind == "leased"
    tally.note = "routine held, urgent admitted; in-flight lease recall is not implemented"


def t8(orch, config, rng, tally):
    held = [lease_it(orch, 20, "Deliver a blanket to ward 5, routine.", Tally())]    # one of the 3 west robots is busy: 2 free
    outs, tasks = [], {}
    for i in range(3):
        t, out = lease_it(orch, i + 1, "Deliver a blanket to ward 5, routine.", tally)
        outs.append(out.kind); tasks[t["task_id"]] = t
    robots = outs
    leased_n = robots.count("leased")
    held[0][0]["status"] = "completed"; orch.finish(held[0][0])
    done = orch.drain(tasks.get)
    tally.add(type("O", (), {"llm_calls": 0, "tokens_in": 0, "tokens_out": 0})())
    tally.ok = leased_n == 2 and robots.count("queued") == 1 and len(done) == 1
    tally.note = f"{leased_n} leased now, {robots.count('queued')} queued, {len(done)} released after a robot freed"


TASKS = {"T1": t1, "T2": t2, "T3": t3, "T4": t4, "T6": t6, "T8": t8}
UNSUPPORTED = {"T5": "cross-wing hand-over (SC-2) not built yet", "T7": "inventory sweep: no survey task template yet"}


# -- stats + output ------------------------------------------------------------------------------------------------------

def ci95(xs, rng):
    if len(xs) < 2:
        return (xs[0], xs[0]) if xs else (0.0, 0.0)
    boots = sorted(statistics.median(rng.choices(xs, k=len(xs))) for _ in range(500))
    return boots[12], boots[487]


def run(seeds: int, price_in: float, price_out: float):
    rows = []
    for seed in range(seeds):
        for tid, fn in TASKS.items():
            for config in CONFIGS:
                with tempfile.TemporaryDirectory() as d:
                    clock = FixedClock()
                    orch = new_orch(config, Path(d), clock)
                    for cid in orch.fleet.clusters:
                        orch.fleet.heartbeat(cid, {})
                    tally = Tally()
                    fn(orch, config, random.Random(f"{tid}-{seed}"), tally)
                    cost = tally.tin * price_in + tally.tout * price_out + tally.cloud_decisions * FLAT_PER_DECISION
                    latency = tally.calls * LLM_LATENCY_S + tally.cloud_decisions * WAN_RTT_S
                    rows.append({"seed": seed, "task": tid, "config": config, "success": int(tally.ok),
                                 "llm_calls": tally.calls, "tokens_in": tally.tin, "tokens_out": tally.tout,
                                 "cloud_decisions": tally.cloud_decisions, "cost_usd": round(cost, 9),
                                 "modelled_latency_s": round(latency, 3), "note": tally.note})
    return rows


def summarise(rows, seeds):
    rng = random.Random(0)
    out = {}
    for tid in TASKS:
        for config in CONFIGS:
            rs = [r for r in rows if r["task"] == tid and r["config"] == config]
            calls = [r["llm_calls"] for r in rs]; cost = [r["cost_usd"] for r in rs]; lat = [r["modelled_latency_s"] for r in rs]
            out[f"{tid}/{config}"] = {
                "success_rate": sum(r["success"] for r in rs) / len(rs),
                "llm_calls_median": statistics.median(calls), "llm_calls_ci95": ci95(calls, rng),
                "cost_usd_median": statistics.median(cost), "cost_usd_ci95": ci95(cost, rng),
                "modelled_latency_s_median": statistics.median(lat), "n": len(rs), "note": rs[0]["note"]}
    for tid, why in UNSUPPORTED.items():
        out[tid] = {"unsupported": why}
    c1 = sum(r["llm_calls"] for r in rows if r["config"] == "C1"); c2 = sum(r["llm_calls"] for r in rows if r["config"] == "C2")
    out["NFR-4"] = {"c1_llm_calls": c1, "c2_llm_calls": c2, "ratio_c1_over_c2": round(c1 / c2, 3) if c2 else None,
                    "target": "<= 0.5", "met": bool(c2) and c1 / c2 <= 0.5}
    return out


COLORS = {"C1": "#2563eb", "C2": "#dc2626", "C3": "#6b7280"}


def bar_svg(title, unit, summary, key, path):
    tasks = list(TASKS); w, h, left, top = 760, 360, 60, 40
    vals = [summary[f"{t}/{c}"][key] for t in tasks for c in CONFIGS]
    top_v = max(vals) or 1
    g = w - left - 20; slot = g / len(tasks); bw = slot / 4
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" font-family="sans-serif" font-size="11">',
             f'<rect width="{w}" height="{h}" fill="white"/><text x="{left}" y="22" font-size="14" font-weight="bold">{title}</text>']
    base = h - 50
    parts.append(f'<line x1="{left}" y1="{base}" x2="{w-20}" y2="{base}" stroke="#999"/>')
    for i, t in enumerate(tasks):
        x0 = left + i * slot + bw / 2
        for j, c in enumerate(CONFIGS):
            v = summary[f"{t}/{c}"][key]; bh = (base - top) * v / top_v
            parts.append(f'<rect x="{x0 + j*bw:.1f}" y="{base-bh:.1f}" width="{bw-2:.1f}" height="{bh:.1f}" fill="{COLORS[c]}"><title>{t} {c}: {v}</title></rect>')
        parts.append(f'<text x="{x0 + 1.5*bw:.1f}" y="{base+16}" text-anchor="middle">{t}</text>')
    parts.append(f'<text x="{left-6}" y="{top+4}" text-anchor="end">{top_v:.3g}</text><text x="{left-6}" y="{base}" text-anchor="end">0</text>')
    parts.append(f'<text x="{left}" y="{h-8}" fill="#555">{unit}</text>')
    for j, c in enumerate(CONFIGS):
        parts.append(f'<rect x="{w-170+j*50}" y="12" width="10" height="10" fill="{COLORS[c]}"/><text x="{w-156+j*50}" y="21">{c}</text>')
    parts.append("</svg>")
    path.write_text("\n".join(parts), encoding="utf-8")


def tradeoff_svg(summary, path):
    w, h, left, base = 520, 360, 70, 310
    pts = {c: (sum(summary[f"{t}/{c}"]["cost_usd_median"] for t in TASKS) / len(TASKS),
               sum(summary[f"{t}/{c}"]["modelled_latency_s_median"] for t in TASKS) / len(TASKS),
               sum(summary[f"{t}/{c}"]["success_rate"] for t in TASKS) / len(TASKS)) for c in CONFIGS}
    mx_c = max(p[0] for p in pts.values()) or 1; mx_l = max(p[1] for p in pts.values()) or 1
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" font-family="sans-serif" font-size="11">',
             f'<rect width="{w}" height="{h}" fill="white"/><text x="{left}" y="22" font-size="14" font-weight="bold">Latency vs cost per task (mean over tasks)</text>',
             f'<line x1="{left}" y1="{base}" x2="{w-20}" y2="{base}" stroke="#999"/><line x1="{left}" y1="40" x2="{left}" y2="{base}" stroke="#999"/>',
             f'<text x="{(left+w)/2:.0f}" y="{h-8}" text-anchor="middle">cost per task, USD (max {mx_c:.2g})</text>',
             f'<text x="14" y="{(40+base)/2:.0f}" transform="rotate(-90 14 {(40+base)/2:.0f})" text-anchor="middle">modelled decision latency, s (max {mx_l:.3g})</text>']
    for c, (cost, lat, ok) in pts.items():
        x = left + 20 + (w - left - 60) * cost / mx_c; y = base - 20 - (base - 60) * lat / mx_l
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="8" fill="{COLORS[c]}"><title>{c}: ${cost:.2g}, {lat:.3g}s, success {ok:.0%}</title></circle>'
                     f'<text x="{x+12:.1f}" y="{y+4:.1f}">{c} ({ok:.0%} ok)</text>')
    parts.append("</svg>")
    path.write_text("\n".join(parts), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--seeds", type=int, default=10)
    ap.add_argument("--out", default=str(ROOT / "results" / "e3"))
    ap.add_argument("--price-in", type=float, default=PRICE_IN)
    ap.add_argument("--price-out", type=float, default=PRICE_OUT)
    a = ap.parse_args()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    rows = run(a.seeds, a.price_in, a.price_out)
    summary = summarise(rows, a.seeds)
    (out / "results.json").write_text(json.dumps({"seeds": a.seeds, "summary": summary, "rows": rows}, indent=1, sort_keys=True))
    with (out / "results.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    bar_svg("Cloud LLM calls per task", "median over seeds", summary, "llm_calls_median", out / "llm_calls.svg")
    bar_svg("Cost per task (USD)", "median over seeds", summary, "cost_usd_median", out / "cost.svg")
    tradeoff_svg(summary, out / "tradeoff.svg")
    print(f"{'task':<5}{'cfg':<4}{'ok':>6}{'calls':>7}{'cost $':>13}{'latency s':>11}  note")
    for tid in TASKS:
        for c in CONFIGS:
            s = summary[f"{tid}/{c}"]
            print(f"{tid:<5}{c:<4}{s['success_rate']:>6.0%}{s['llm_calls_median']:>7.0f}{s['cost_usd_median']:>13.2e}"
                  f"{s['modelled_latency_s_median']:>11.2f}  {s['note'] if c == 'C1' else ''}")
    for tid, why in UNSUPPORTED.items():
        print(f"{tid:<5}unsupported: {why}")
    n = summary["NFR-4"]
    print(f"\nNFR-4: C1 {n['c1_llm_calls']} model calls vs C2 {n['c2_llm_calls']} (ratio {n['ratio_c1_over_c2']}, target <= 0.5): {'MET' if n['met'] else 'NOT MET'}")
    print(f"wrote {out}/results.json, results.csv, llm_calls.svg, cost.svg, tradeoff.svg")


if __name__ == "__main__":
    main()
