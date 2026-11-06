"""C2 - Plain-language rules and the shared map (FR-17).

Part A, rules: the 10 statements in evaluation/e2/rules.json, each on a fresh cloud (a 'lift' statement starts with
the restriction already declared). Right = the statement becomes exactly the labelled restriction (zone, kind, declared
or lifted) and the labelled items are moved out (child tasks). Then a routine and an urgent delivery to the ward check
the rule's EFFECT: routine is held while the ward is isolated, urgent still goes in (the pack's exemption).
Runs in-process so a restriction can be pre-declared; model = stand-in by default, real Bedrock with --llm bedrock
(needs AWS credentials).

Part B, shared map: 20 scripted edge reports (a corridor blocked or a door closed on a live lease, via
experiments/cloud/scenarios.py) against a running cloud. Measured: time until /map.json shows the closure, and whether a
fresh request to the same place gets a lease that avoids the zone (or waits rather than routing through it).

    python experiments/cloud/c2_rules_map.py --target local                      # part A stand-in, part B local
    python experiments/cloud/c2_rules_map.py --target deployed --skip-rules      # part B on the deployed cloud
    python experiments/cloud/c2_rules_map.py --llm bedrock --skip-map            # part A on real Bedrock (laptop)
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402
import scenarios  # noqa: E402


def orch_for_rules(llm: str):
    os.environ["NIMBUS_PACK"] = "hospital_sim6"
    os.environ["NIMBUS_MODE"] = "C1"
    os.environ["NIMBUS_LLM"] = "bedrock" if llm == "bedrock" else "fake"
    if llm == "bedrock":
        os.environ.setdefault("NIMBUS_BEDROCK_MODEL", "apac.amazon.nova-lite-v1:0")
    sys.path.insert(0, str(lib.ROOT))
    from cloud.clock import FixedClock
    from cloud.orchestrator import Orchestrator
    orch = Orchestrator.from_env(Path(tempfile.mkdtemp(prefix="c2-")), clock=FixedClock(), backend="local")
    for cid in orch.fleet.clusters:
        orch.fleet.heartbeat(cid, {})
    return orch


def part_a(rd, llm: str) -> list[dict]:
    rules = json.loads((lib.ROOT / "evaluation" / "e2" / "rules.json").read_text())["rules"]
    out_rows = []
    for i, r in enumerate(rules):
        orch = orch_for_rules(llm)
        g = r["gold"]
        if g["lift"]:
            orch.policies.declare(g["zone"], g["kind"])
        task = {"task_id": "task-000001", "request_text": r["text"], "status": "received"}
        out = orch.handle_request(task, r["text"])
        active = orch.policies.restrictions()
        applied = (g["zone"] not in active) if g["lift"] else (active.get(g["zone"], {}).get("kind") == g["kind"])
        moved = sorted({c["item"] for c in (out.children or [])})
        items_ok = (moved == sorted(g["extract_items"])) if not g["lift"] else True
        ok = out.kind == "statement" and applied and items_ok
        effect = {}
        if not g["lift"]:                         # the rule's effect on deliveries to that ward
            ward = {"west_waiting_approach": "2C", "east_waiting_approach": "2B", "south_room_approach": "ward 3"}[g["zone"]]
            for n, (pri, text) in enumerate((("routine", f"Deliver a blanket to {ward}, routine."), ("urgent", f"Deliver a blanket to {ward}, urgent."))):
                t = {"task_id": f"task-{n + 2:06d}", "request_text": text, "status": "received"}
                o = orch.handle_request(t, text)
                effect[pri] = o.kind
        effect_ok = (effect.get("routine") in ("queued", "rejected") and effect.get("urgent") == "leased") if effect else None
        row = {"rule": r["id"], "text": r["text"], "gold": g, "outcome": out.kind, "restrictions_after": active,
               "items_moved_out": moved, "applied_correctly": applied, "items_correct": items_ok, "correct": ok,
               "effect": effect, "effect_correct": effect_ok, "model": getattr(orch.llm, "model_id", "?"),
               "source": "real_bedrock" if llm == "bedrock" else "mock_llm"}
        rd.raw("rules", row)
        out_rows.append(row)
        print(f"[C2] {r['id']}: correct={ok} effect={effect}")
    return out_rows


def part_b(rd, target_spec: str, n: int, seed: int) -> list[dict]:
    target = lib.open_target(target_spec)
    rd.env.update({f"map_{k}": v for k, v in target.info().items()})
    edges = lib.Edges(target, on_lease=lambda e, c, l: None).start()
    rng = random.Random(seed)
    recs = []
    try:
        for i in range(n):
            kind = ("blocked_with_detour", "blocked_no_detour", "closed_door")[i % 3]
            rec = scenarios.run_incident(target, edges, kind, rng, probe=True)
            rec.update(event=i, source=target.source)
            rd.raw("map_changes", rec)
            recs.append(rec)
            print(f"[C2] map {i + 1}/{n} {kind} {rec.get('zone')}: shown after {rec.get('map_reflected_s')} s, "
                  f"next request avoids it: {rec.get('probe_avoids_zone')}")
    finally:
        edges.stop()
        target.close()
    return recs


def main(argv=None) -> int:
    ap = lib.common_args(argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter), "local")
    ap.add_argument("--llm", choices=["standin", "bedrock"], default="standin", help="model for part A (in-process)")
    ap.add_argument("--events", type=int, default=20)
    ap.add_argument("--skip-rules", action="store_true")
    ap.add_argument("--skip-map", action="store_true")
    a = ap.parse_args(argv)
    suffix = "" if (a.llm == "bedrock" or a.target not in ("local",)) else "_standin"
    rd = lib.ResultDir("C2" + suffix, "Plain-language rules and shared map (FR-17)", None)
    rd.reset_raw()
    rd.env.update({"source": "real_bedrock" if a.llm == "bedrock" else "mock_llm", "rules_model": a.llm, "map_target": a.target})
    rules = [] if a.skip_rules else part_a(rd, a.llm)
    maps = [] if a.skip_map else part_b(rd, a.target, a.events, a.seed)
    summary, names, measured, ok = [], [], [], True
    plt = lib.plt()
    if rules:
        k = sum(r["correct"] for r in rules)
        eff = [r for r in rules if r["effect_correct"] is not None]
        ke = sum(r["effect_correct"] for r in eff)
        summary.append({"part": "A rules", "n": len(rules), "correct": k, "effect_checked": len(eff), "effect_correct": ke,
                        "source": rules[0]["source"], "model": rules[0]["model"]})
        measured.append(f"rules applied correctly {k}/{len(rules)} ({rules[0]['model']}); effect on deliveries correct {ke}/{len(eff)}")
        ok &= k == len(rules)
        fig, ax = plt.subplots(figsize=(7, 3.4))
        ax.barh([r["rule"] for r in rules][::-1], [1] * len(rules), color=["#2ca02c" if r["correct"] else "#d62728" for r in rules][::-1])
        for y, r in enumerate(rules[::-1]):
            ax.text(0.02, y, (r["text"][:70] + ("  [effect ok]" if r["effect_correct"] else "  [effect wrong]" if r["effect_correct"] is False else "")),
                    va="center", fontsize=7, color="white")
        ax.set_xticks([]); ax.set_title(f"C2 part A: {k}/{len(rules)} plain-language rules applied (green = right)")
        names.append(lib.save(fig, rd, "rules_outcome"))
    if maps:
        shown = [r["map_reflected_s"] for r in maps if r.get("map_reflected_s") is not None]
        within = sum(1 for x in shown if x <= 300)
        probe = [r for r in maps if r.get("probe_avoids_zone") is not None]
        avoid = sum(1 for r in probe if r["probe_avoids_zone"])
        summary.append({"part": "B map", "n": len(maps), "shown_on_map": len(shown), "shown_within_5_min": within,
                        "map_p50_s": lib.r(lib.percentile(shown, 50)), "map_p95_s": lib.r(lib.percentile(shown, 95)),
                        "next_request_checked": len(probe), "next_request_avoids_zone": avoid, "source": maps[0]["source"]})
        measured.append(f"map changes shown within 5 min {within}/{len(maps)} (p95 {lib.percentile(shown, 95):.2f} s); "
                        f"next request to the same place avoided the closed zone {avoid}/{len(probe)}")
        ok &= within / len(maps) >= 0.8 and avoid == len(probe)
        fig, ax = plt.subplots()
        lib.ecdf(ax, shown, "edge report -> closure on /map.json")
        lib.pass_line(ax, 300, "FR-17: within 5 min", axis="v")
        ax.set_xscale("log"); ax.set_xlabel("seconds (log)"); ax.set_ylabel("share of map changes"); ax.legend(fontsize=8)
        ax.set_title(f"C2 part B: map updates ({len(shown)}/{len(maps)} shown)")
        names.append(lib.save(fig, rd, "map_update_cdf"))
    rd.summary(summary)
    rd.finish(claim="Plain-language rules become enforced restrictions, and robot reports update one shared map that later plans respect.",
              pass_bar="Scripted changes reflected in the map within about 5 min in >= 80% of cases; 10 test rules applied correctly (FR-17).",
              verdict="PASS" if ok else "FAIL", measured="; ".join(measured),
              notes=["Part A runs in-process so a restriction can be pre-declared for the 'lift' statements; part B goes through the HTTP API.",
                     "Every map change is reopened and its tasks cancelled before the next one."],
              plots=names)
    return 0


if __name__ == "__main__":
    sys.exit(main())
