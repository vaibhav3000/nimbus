"""E3: C1 vs C2 vs C3 on tasks T1-T8 (PRD Sec. 7 Table 3), cloud side, in process, on P1's Gazebo map.

Drives the real Orchestrator on a fixed clock with a seeded workload, so a run is repeatable (NFR-6). For each
configuration it plays the edge's part from the lease the cloud issued:

  C1  the edge decides locally (its small model, ~0.5 s per step, no cloud call) and only escalates when it must;
  C2  the edge is a thin relay: every non-reflex step is a cloud round trip (decide_step, one model call);
  C3  no model anywhere: forms, the classical allocator and hand-written rules.

Per run it records success, whether the deadline was met on a MODELLED clock, model calls and tokens, WAN bytes (the size of
every message between cloud and edge), a cost per task from the price table, and the modelled decision latency. Latency and
completion time are modelled from constants below (not measured); calls, tokens, bytes and success come from the real code.

    python scripts/run_e3.py                          # offline stand-in model, 10 seeds, writes results/e3/
    python scripts/run_e3.py --seeds 3 --out /tmp/e3
    NIMBUS_LLM=bedrock NIMBUS_BEDROCK_MODEL=apac.amazon.nova-lite-v1:0 \
      NIMBUS_CASSETTE=data/e3.cassette.json NIMBUS_CASSETTE_MODE=record python scripts/run_e3.py --seeds 1   # you run this (AWS)
    NIMBUS_LLM=bedrock NIMBUS_CASSETTE=data/e3.cassette.json NIMBUS_CASSETTE_MODE=replay python scripts/run_e3.py --seeds 10

Writes results.json, results.csv and SVG plots. `python scripts/e3_report.py` turns results.json into docs/e3-results.md.
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
os.environ.setdefault("NIMBUS_PACK", "hospital_sim")

from cloud import mapview  # noqa: E402
from cloud.clock import FixedClock, parse_iso  # noqa: E402
from cloud.orchestrator import Orchestrator  # noqa: E402

CONFIGS = ("C1", "C2", "C3")
PRICE_IN, PRICE_OUT = 0.06e-6, 0.24e-6          # Nova Lite on-demand, USD per token (symbolic in the PRD; --price-in/--price-out)
LLM_LATENCY_S, WAN_RTT_S = 2.5, 0.08            # a Bedrock call measured 2-3 s in MS1; a good-network round trip
EDGE_STEP_S = {"C1": 0.5, "C2": LLM_LATENCY_S + 2 * WAN_RTT_S, "C3": 0.01}    # per non-reflex edge decision (E1: SLM p95 0.495 s)
FLAT_PER_DECISION = 2e-7                        # Fargate/DynamoDB/IoT share per cloud decision (stand-in for the Sec. 7 terms)
STEP_REQUEST_BYTES, STEP_REPLY_BYTES = 420, 180  # a C2 relay call: the event, then proceed/reroute


def size(obj) -> int:
    return len(json.dumps(obj, separators=(",", ":")))


class Tally:
    def __init__(self):
        self.calls = self.tin = self.tout = self.cloud_decisions = self.wan = 0
        self.ok = self.deadline_ok = False
        self.complete_s = 0.0
        self.note = ""

    def outcome(self, out) -> None:
        self.calls += getattr(out, "llm_calls", 0)
        self.tin += getattr(out, "tokens_in", 0)
        self.tout += getattr(out, "tokens_out", 0)
        self.cloud_decisions += 1
        self.wan += sum(size(p) for _, p in getattr(out, "publish", []))

    def edge_message(self, payload) -> None:
        self.wan += size(payload)


def new_orch(config: str, workdir: Path, clock: FixedClock) -> Orchestrator:
    previous = os.environ.get("NIMBUS_MODE")
    os.environ["NIMBUS_MODE"] = config          # read once, at construction; restored so nothing else in the process is affected
    try:
        orch = Orchestrator.from_env(workdir, clock=clock)
    finally:
        if previous is None:
            os.environ.pop("NIMBUS_MODE", None)
        else:
            os.environ["NIMBUS_MODE"] = previous
    for cid in orch.fleet.clusters:
        orch.fleet.heartbeat(cid, {})
    return orch


def task(n: int, text: str) -> dict:
    return {"task_id": f"task-{n:06d}", "request_text": text, "status": "received"}


def ask(orch, n, text, tally, answer=None):
    t = task(n, text)
    out = orch.handle_request(t, text)
    tally.outcome(out)
    if out.kind == "clarify" and answer:
        out = orch.handle_request(t, text, question=out.question, answer=answer)
        tally.outcome(out)
    t.update(status=out.kind, spec=out.spec, lease=out.lease, reservation=out.reservation, legs=out.legs,
             leg_index=out.leg_index or 0, request_text=text)
    return t, out


def travel_s(orch, lease: dict, start: str | None = None) -> float:
    """Robot time to run a lease: drive at the lease's speed limit between its places, plus its dwell and instant steps."""
    s = 0.0
    here = start or orch.fleet.robot_zone(lease["assigned_robots"][0])
    for step in mapview._plan(orch.topo, lease, here):
        if step["type"] == "drive":
            path = step["path"] if here is None or here == step["path"][0] else [here, *step["path"]]
            s += sum(orch.topo.path_length([a, b]) for a, b in zip(path, path[1:])) / max(0.05, lease["speed_limit"])
            here = step["to"]
        else:
            s += step["seconds"]
    return s


def relay(orch, config, t, lease, tally, blocked=()):
    """The edge's non-reflex decisions for one lease. C2 asks the cloud before every step; C1 and C3 decide on the edge."""
    steps = len(lease["task_sequence"])
    tally.complete_s += steps * EDGE_STEP_S[config]
    if config != "C2":
        return True
    ok = True
    here = orch.fleet.robot_zone(lease["assigned_robots"][0]) or lease["allowed_zones"][0]
    last_go = max((i for i, s in enumerate(lease["task_sequence"]) if s["cmd"] == "go_to"), default=-1)   # the delivery leg
    for i, step in enumerate(lease["task_sequence"]):
        target = (orch.topo.anchor_of(step["params"]["to"]) or step["params"]["to"]) if step["cmd"] == "go_to" else here
        ev = {"step_index": i, "cmd": step["cmd"], "zone": here, "target": target, "situation": "next step"}
        if blocked and i == last_go:
            ev["blocked_zones"] = list(blocked)
        here = target
        res = orch.decide_step(t["task_id"], lease["version"], lease["cluster_id"], ev)
        tally.calls += res["calls"]; tally.tin += res["tokens_in"]; tally.tout += res["tokens_out"]
        tally.cloud_decisions += 1; tally.wan += STEP_REQUEST_BYTES + STEP_REPLY_BYTES
        ok = ok and res["decision"]["action"] != "abort"
    return ok


def finish_run(orch, tally, t, out, config):
    """Success also needs the work to fit its deadline on the modelled clock."""
    spec = out.spec or t.get("spec") or {}
    deadline_s = spec.get("deadline_minutes", 60) * 60
    decision_s = tally.calls * LLM_LATENCY_S + tally.cloud_decisions * WAN_RTT_S
    tally.complete_s += decision_s + (travel_s(orch, out.lease) if out.lease else 0)
    tally.deadline_ok = tally.ok and tally.complete_s <= deadline_s


# -- the tasks (PRD Table 3), on P1's map -------------------------------------------------------------------------------------

def t1(orch, config, rng, tally):
    t, out = ask(orch, 1, "Deliver a wheelchair to ward 3, routine.", tally)
    tally.ok = out.kind == "leased" and relay(orch, config, t, out.lease, tally)
    if out.lease:
        finish_run(orch, tally, t, out, config)


def t2(orch, config, rng, tally):
    for i in range(rng.randint(1, 2)):                                 # background traffic first
        ask(orch, 10 + i, "Deliver a blanket to ward 2C, routine.", Tally())
    t, out = ask(orch, 1, "Need a monitor at ward 3, urgent.", tally)
    tally.ok = out.kind == "leased" and out.lease["priority"] == "urgent" and relay(orch, config, t, out.lease, tally)
    if out.lease:
        finish_run(orch, tally, t, out, config)


def t3(orch, config, rng, tally):
    t, out = ask(orch, 1, "a pump to 2B", tally, answer="Yes, a paediatric pump please.")
    tally.ok = out.kind == "leased" and relay(orch, config, t, out.lease, tally)
    if out.lease:
        finish_run(orch, tally, t, out, config)


def t4(orch, config, rng, tally):
    # picked up at the nurse station, a hub with two ways into the ward (the store's only way in is one corridor)
    t, out = ask(orch, 1, "Move a monitor from nurse-station to 2B, routine.", tally)
    if out.kind != "leased":
        return
    # the corridor on the route is blocked: C1/C3 detour on the edge INSIDE the lease; C2 asks the cloud at every step
    lease, interior = out.lease, [z for z in out.lease["allowed_zones"] if z.endswith("_mid_hall")]
    detour = bool(interior) and orch.topo.shortest_path(lease["allowed_zones"][0], out.spec["destination_anchor"],
                                                       allowed=set(lease["allowed_zones"]), avoid=set(interior)) is not None
    tally.ok = relay(orch, config, t, lease, tally, blocked=interior) and detour
    tally.note = "detour exists inside the lease" if detour else "no detour inside the lease"
    finish_run(orch, tally, t, out, config)


def t5(orch, config, rng, tally):
    t, out = ask(orch, 1, "Move a monitor from 2B to 2C, routine.", tally)
    if out.kind != "leased" or not out.legs:
        return
    orch.fleet.heartbeat(out.lease["cluster_id"], {"robots": [{"id": out.lease["assigned_robots"][0], "state": "busy", "zone": out.legs[0]["source"]}]})
    done = orch.advance_leg(t)                                          # leg 1 finished: leg 2 goes to the other cluster
    tally.outcome(done)
    if done.kind != "leased":
        return
    t.update(spec=out.spec, lease=done.lease, legs=out.legs, leg_index=1, status="leased")
    orch.fleet.heartbeat(done.lease["cluster_id"], {"robots": [{"id": done.lease["assigned_robots"][0], "state": "busy", "zone": "north_hall"}]})
    ev = {"what_happened": "west_north_gate closed", "where": "west_north_gate", "robots_saw": ["door shut"],
          "segments": [{"segment_id": "west_north_gate", "state": "closed"}]}
    tally.edge_message(ev)
    react = orch.react.on_event(t, ev)                                  # C3 uses the rules; C1/C2 the model (the cloud, both)
    tally.calls += react.llm_calls; tally.tin += react.tokens_in; tally.tout += react.tokens_out; tally.cloud_decisions += 1
    new = react.outcome
    if new is not None:
        tally.wan += sum(size(p) for _, p in new.publish)
    tally.ok = new is not None and new.kind == "leased" and "west_north_gate" in new.lease["forbidden_zones"] \
        and relay(orch, config, t, new.lease, tally)
    if new is not None and new.lease:
        finish_run(orch, tally, t, new, config)


def t6(orch, config, rng, tally):
    routine, r_out = ask(orch, 1, "Deliver a blanket to ward 2C, routine.", Tally())
    orch.load_task = {routine["task_id"]: routine}.get
    s, out = ask(orch, 2, "Ward 2C is now isolation. Get the spare pumps out, hold routine deliveries there, urgent ones can still go in.", tally)
    held = [o for _, o in out.others if o.kind == "held"]
    tally.wan += sum(size(p) for _, o in out.others for _, p in o.publish)
    urgent, u_out = ask(orch, 3, "Deliver a blanket to ward 2C, urgent.", tally)
    again, a_out = ask(orch, 4, "Deliver a blanket to ward 2C, routine.", tally)
    tally.ok = bool(held) and a_out.kind == "queued" and u_out.kind in ("leased", "queued") and len(out.children) >= 1
    tally.note = f"{len(held)} in-flight held, {len(out.children)} extraction task(s), routine queued, urgent {u_out.kind}"
    tally.complete_s = (travel_s(orch, u_out.lease) if u_out.lease else 0) + tally.calls * LLM_LATENCY_S
    tally.deadline_ok = tally.ok and tally.complete_s <= 12 * 60


def t7(orch, config, rng, tally):
    t, out = ask(orch, 1, "Count the idle infusion pumps.", tally)
    if out.kind != "leased":
        return
    steps = len(out.lease["task_sequence"])
    tally.complete_s += steps * EDGE_STEP_S[config] + (travel_s(orch, out.lease))
    if config == "C2":
        relay(orch, config, t, out.lease, tally)
        tally.complete_s += 0
    # the edge filters: C1 returns a short list of counts; C2 and C3 have no edge model, so they ship every photo's result
    report = {"reports": [{"robot": out.lease["assigned_robots"][0], "counts": {p: {"infusion_pump": 1} for p in out.spec["targets"]}}]}
    tally.edge_message(report if config == "C1" else {"raw": [{"place": p, "photo_bytes": 200_000} for p in out.spec["targets"]]})
    tally.wan += 0 if config == "C1" else 200_000 * len(out.spec["targets"])
    t.update(spec=out.spec, lease=out.lease)
    text, n = orch.surveyor.merge(t, json.dumps(report))
    tally.ok = n >= 1 and "Survey done" in text
    deadline = out.spec["deadline_minutes"] * 60
    tally.complete_s += tally.calls * LLM_LATENCY_S + tally.cloud_decisions * WAN_RTT_S
    tally.deadline_ok = tally.ok and tally.complete_s <= deadline
    tally.note = "edge-filtered counts" if config == "C1" else "raw results shipped (no edge model)"


def t8(orch, config, rng, tally):
    """Three simultaneous requests, two robots: two run, one waits, and runs when a robot frees (no robot ever holds two)."""
    tasks, outs = {}, []
    for i in range(3):
        t, out = ask(orch, i + 1, "Deliver a blanket to ward 3, routine.", tally)
        outs.append(out.kind); tasks[t["task_id"]] = t
    first = next(t for t in tasks.values() if t["status"] == "leased")
    orch.finish(first); first["status"] = "completed"
    done = orch.drain(tasks.get)
    for _, o in done:
        tally.outcome(o)
    robots = [t["lease"]["assigned_robots"][0] for t in tasks.values() if t["status"] == "leased"] + [o.lease["assigned_robots"][0] for _, o in done]
    tally.ok = outs.count("leased") == 2 and outs.count("queued") == 1 and len(done) == 1 and len(robots) == len(set(robots))
    tally.note = f"{outs.count('leased')} leased now, {outs.count('queued')} queued, {len(done)} released after a robot freed"
    tally.complete_s = tally.calls * LLM_LATENCY_S
    tally.deadline_ok = tally.ok


TASKS = {"T1": t1, "T2": t2, "T3": t3, "T4": t4, "T5": t5, "T6": t6, "T7": t7, "T8": t8}


# -- run, summarise, plot ------------------------------------------------------------------------------------------------------

def ci95(xs, rng):
    if len(xs) < 2:
        return (xs[0], xs[0]) if xs else (0.0, 0.0)
    boots = sorted(statistics.median(rng.choices(xs, k=len(xs))) for _ in range(500))
    return boots[12], boots[487]


def run(seeds: int, price_in: float, price_out: float, only: list[str] | None = None):
    rows = []
    for seed in range(seeds):
        for tid, fn in TASKS.items():
            if only and tid not in only:
                continue
            for config in CONFIGS:
                with tempfile.TemporaryDirectory() as d:
                    orch = new_orch(config, Path(d), FixedClock())
                    tally = Tally()
                    fn(orch, config, random.Random(f"{tid}-{seed}"), tally)
                    cost = tally.tin * price_in + tally.tout * price_out + tally.cloud_decisions * FLAT_PER_DECISION
                    latency = tally.calls * LLM_LATENCY_S + tally.cloud_decisions * WAN_RTT_S
                    rows.append({"seed": seed, "task": tid, "config": config, "success": int(tally.ok), "deadline_met": int(tally.deadline_ok),
                                 "llm_calls": tally.calls, "tokens_in": tally.tin, "tokens_out": tally.tout,
                                 "cloud_decisions": tally.cloud_decisions, "wan_bytes": tally.wan, "cost_usd": round(cost, 9),
                                 "modelled_latency_s": round(latency, 3), "modelled_completion_s": round(tally.complete_s, 1), "note": tally.note})
    return rows


def summarise(rows, seeds):
    rng = random.Random(0)
    out = {}
    for tid in TASKS:
        for config in CONFIGS:
            rs = [r for r in rows if r["task"] == tid and r["config"] == config]
            if not rs:
                continue
            calls = [r["llm_calls"] for r in rs]; cost = [r["cost_usd"] for r in rs]
            out[f"{tid}/{config}"] = {
                "success_rate": sum(r["success"] for r in rs) / len(rs), "deadline_rate": sum(r["deadline_met"] for r in rs) / len(rs),
                "llm_calls_median": statistics.median(calls), "llm_calls_ci95": ci95(calls, rng),
                "cost_usd_median": statistics.median(cost), "cost_usd_ci95": ci95(cost, rng),
                "wan_bytes_median": statistics.median(r["wan_bytes"] for r in rs),
                "modelled_latency_s_median": statistics.median(r["modelled_latency_s"] for r in rs),
                "modelled_completion_s_median": statistics.median(r["modelled_completion_s"] for r in rs),
                "n": len(rs), "note": rs[0]["note"]}
    c1 = sum(r["llm_calls"] for r in rows if r["config"] == "C1"); c2 = sum(r["llm_calls"] for r in rows if r["config"] == "C2")
    out["NFR-4"] = {"c1_llm_calls": c1, "c2_llm_calls": c2, "ratio_c1_over_c2": round(c1 / c2, 3) if c2 else None,
                    "target": "<= 0.5", "met": bool(c2) and c1 / c2 <= 0.5}
    return out


COLORS = {"C1": "#2563eb", "C2": "#dc2626", "C3": "#6b7280"}


def bar_svg(title, unit, summary, key, path, scale=1.0):
    tasks = [t for t in TASKS if f"{t}/C1" in summary]
    w, h, left, top = 820, 360, 60, 40
    vals = [summary[f"{t}/{c}"][key] * scale for t in tasks for c in CONFIGS]
    top_v = max(vals) or 1
    slot = (w - left - 20) / len(tasks); bw = slot / 4
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" font-family="sans-serif" font-size="11">',
             f'<rect width="{w}" height="{h}" fill="white"/><text x="{left}" y="22" font-size="14" font-weight="bold">{title}</text>']
    base = h - 50
    parts.append(f'<line x1="{left}" y1="{base}" x2="{w-20}" y2="{base}" stroke="#999"/>')
    for i, t in enumerate(tasks):
        x0 = left + i * slot + bw / 2
        for j, c in enumerate(CONFIGS):
            v = summary[f"{t}/{c}"][key] * scale; bh = (base - top) * v / top_v
            parts.append(f'<rect x="{x0 + j*bw:.1f}" y="{base-bh:.1f}" width="{bw-2:.1f}" height="{bh:.1f}" fill="{COLORS[c]}"><title>{t} {c}: {v:.4g}</title></rect>')
        parts.append(f'<text x="{x0 + 1.5*bw:.1f}" y="{base+16}" text-anchor="middle">{t}</text>')
    parts.append(f'<text x="{left-6}" y="{top+4}" text-anchor="end">{top_v:.3g}</text><text x="{left-6}" y="{base}" text-anchor="end">0</text>')
    parts.append(f'<text x="{left}" y="{h-8}" fill="#555">{unit}</text>')
    for j, c in enumerate(CONFIGS):
        parts.append(f'<rect x="{w-170+j*50}" y="12" width="10" height="10" fill="{COLORS[c]}"/><text x="{w-156+j*50}" y="21">{c}</text>')
    parts.append("</svg>")
    path.write_text("\n".join(parts), encoding="utf-8")


def tradeoff_svg(summary, path):
    w, h, left, base = 520, 360, 70, 310
    tasks = [t for t in TASKS if f"{t}/C1" in summary]
    pts = {c: (sum(summary[f"{t}/{c}"]["cost_usd_median"] for t in tasks) / len(tasks),
               sum(summary[f"{t}/{c}"]["modelled_completion_s_median"] for t in tasks) / len(tasks),
               sum(summary[f"{t}/{c}"]["success_rate"] for t in tasks) / len(tasks)) for c in CONFIGS}
    mx_c = max(p[0] for p in pts.values()) or 1; mx_l = max(p[1] for p in pts.values()) or 1
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" font-family="sans-serif" font-size="11">',
             f'<rect width="{w}" height="{h}" fill="white"/><text x="{left}" y="22" font-size="14" font-weight="bold">Completion time vs cost per task (mean over tasks)</text>',
             f'<line x1="{left}" y1="{base}" x2="{w-20}" y2="{base}" stroke="#999"/><line x1="{left}" y1="40" x2="{left}" y2="{base}" stroke="#999"/>',
             f'<text x="{(left+w)/2:.0f}" y="{h-8}" text-anchor="middle">cost per task, USD (max {mx_c:.2g})</text>',
             f'<text x="14" y="{(40+base)/2:.0f}" transform="rotate(-90 14 {(40+base)/2:.0f})" text-anchor="middle">modelled completion, s (max {mx_l:.3g})</text>']
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
    ap.add_argument("--tasks", default="", help="comma-separated subset, e.g. T1,T5")
    ap.add_argument("--price-in", type=float, default=PRICE_IN)
    ap.add_argument("--price-out", type=float, default=PRICE_OUT)
    a = ap.parse_args()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    only = [t.strip() for t in a.tasks.split(",") if t.strip()] or None
    rows = run(a.seeds, a.price_in, a.price_out, only)
    summary = summarise(rows, a.seeds)
    model = "stand-in" if os.environ.get("NIMBUS_LLM", "fake") != "bedrock" else os.environ.get("NIMBUS_BEDROCK_MODEL", "bedrock")
    (out / "results.json").write_text(json.dumps({"seeds": a.seeds, "pack": os.environ["NIMBUS_PACK"], "model": model, "summary": summary, "rows": rows}, indent=1, sort_keys=True))
    with (out / "results.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    bar_svg("Cloud LLM calls per task", "median over seeds", summary, "llm_calls_median", out / "llm_calls.svg")
    bar_svg("Cost per task (USD x 1e6)", "median over seeds", summary, "cost_usd_median", out / "cost.svg", 1e6)
    bar_svg("WAN bytes per task", "median over seeds", summary, "wan_bytes_median", out / "wan_bytes.svg")
    bar_svg("Task success rate", "fraction of seeds", summary, "success_rate", out / "success.svg")
    bar_svg("Modelled completion time (s)", "median over seeds; modelled, not measured", summary, "modelled_completion_s_median", out / "completion.svg")
    tradeoff_svg(summary, out / "tradeoff.svg")
    print(f"pack {os.environ['NIMBUS_PACK']}, model {model}, {a.seeds} seeds")
    print(f"{'task':<5}{'cfg':<4}{'ok':>6}{'dl':>6}{'calls':>7}{'wan B':>9}{'cost $':>12}{'done s':>9}  note")
    for tid in TASKS:
        for c in CONFIGS:
            s = summary.get(f"{tid}/{c}")
            if s:
                print(f"{tid:<5}{c:<4}{s['success_rate']:>6.0%}{s['deadline_rate']:>6.0%}{s['llm_calls_median']:>7.0f}{s['wan_bytes_median']:>9.0f}"
                      f"{s['cost_usd_median']:>12.2e}{s['modelled_completion_s_median']:>9.0f}  {s['note'] if c == 'C1' else ''}")
    n = summary["NFR-4"]
    print(f"\nNFR-4: C1 {n['c1_llm_calls']} model calls vs C2 {n['c2_llm_calls']} (ratio {n['ratio_c1_over_c2']}, target <= 0.5): {'MET' if n['met'] else 'NOT MET'}")
    print(f"wrote {out}/results.json, results.csv and SVG plots")


if __name__ == "__main__":
    main()
