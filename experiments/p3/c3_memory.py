"""C3 - Memory and habits (FR-19).

The 20 context-dependent cases in evaluation/e2/context.json, each run twice on a fresh local cloud:
  memory ON   build the habit: `setup` times, "Need an infusion pump at 2B-12, urgent." -> the cloud asks (SOP-PAED-01)
              -> the requester answers "Yes, a paediatric pump please." -> lease -> the simulated edge delivers
              (OUTPUT_READY). Then the test request.
  memory OFF  exactly the same, then POST /reset-memory just before the test request (the ablation).
Right = the labelled outcome: habit cases resolved WITHOUT a question to the usual item; controls (no or too little
history) still ask; an explicit "standard" pump is honoured.

Memory resets are only done on a local cloud (never on the shared deployment).
    python experiments/p3/c3_memory.py                    # stand-in model
    python experiments/p3/c3_memory.py --target local-bedrock   # real Bedrock (needs AWS credentials)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402
import scenarios  # noqa: E402

SETUP = "Need an infusion pump at 2B-12, urgent."
ANSWER = "Yes, a paediatric pump please."


def build_habit(target: lib.Target, edges: lib.Edges, n: int) -> list[dict]:
    log = []
    for _ in range(n):
        st, out, _ = target.request(SETUP)
        tid = out.get("task_id")
        edges.tasks.add(tid)
        step = {"task": tid, "first": out.get("status")}
        if out.get("status") == "needs_clarification":
            st, out2, _ = target.api.post(f"/requests/{tid}/answer", {"text": ANSWER, "message_id": f"exp-ans-{uuid.uuid4().hex[:8]}"})
            step["after_answer"] = out2.get("status")
        done = scenarios.wait_for(lambda: target.api.get(f"/tasks/{tid}")[1].get("status") in ("completed", "cancelled", "failed"), 30, 0.2)
        step["final"] = target.api.get(f"/tasks/{tid}")[1].get("status")
        log.append(step)
    return log


def run_case(case: dict, memory: bool, spec: str) -> dict:
    target = lib.open_target(spec)
    edges = lib.Edges(target, work_s=0.2).start()
    try:
        setup = build_habit(target, edges, case["setup"])
        if not memory:
            st, cleared, _ = target.api.post("/reset-memory", {})
        st, out, dt = target.request(case["text"])
        tid = out.get("task_id")
        s, task, _ = target.api.get(f"/tasks/{tid}")
        asked = task.get("status") == "needs_clarification"
        item = (task.get("spec") or {}).get("item")
        g = case["gold"]
        ok = asked if g["ask"] else (not asked and item == g["item"])
        return {"case": case["id"], "kind": case["kind"], "setup": case["setup"], "memory": "on" if memory else "off",
                "text": case["text"], "gold": g, "asked": asked, "item": None if asked else item, "status": task.get("status"),
                "correct": ok, "setup_log": setup, "latency_s": round(dt, 3), "source": target.source,
                "model": target.info().get("model")}
    finally:
        edges.stop()
        target.close()


def main(argv=None) -> int:
    ap = lib.common_args(argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter), "local")
    a = ap.parse_args(argv)
    if a.target not in ("local", "local-bedrock"):
        raise SystemExit("C3 resets memory, so it only runs on a local cloud: --target local or local-bedrock")
    cases = json.loads((lib.ROOT / "evaluation" / "e2" / "context.json").read_text())["cases"]
    rd = lib.ResultDir("C3" if a.target == "local-bedrock" else "C3_standin", "Memory and habits (FR-19)", None)
    rd.reset_raw()
    recs = []
    for mem in (True, False):
        for c in cases:
            rec = run_case(c, mem, a.target)
            rd.raw("cases", rec)
            recs.append(rec)
            print(f"[C3] memory {'on ' if mem else 'off'} {c['id']} {c['kind']}: asked={rec['asked']} item={rec['item']} correct={rec['correct']}")
    rd.env.update({"source": recs[0]["source"], "target": a.target, "model": recs[0]["model"]})
    summary = []
    for mem in ("on", "off"):
        for kind in ("habit", "control", "override", "all"):
            sub = [r for r in recs if r["memory"] == mem and (kind == "all" or r["kind"] == kind)]
            k = sum(r["correct"] for r in sub)
            lo, hi = lib.wilson(k, len(sub))
            summary.append({"memory": mem, "kind": kind, "cases": len(sub), "correct": k, "rate": lib.r(k / len(sub)),
                            "ci_low": lib.r(lo), "ci_high": lib.r(hi), "questions_asked": sum(r["asked"] for r in sub),
                            "source": recs[0]["source"]})
    rd.summary(summary)
    plt = lib.plt()
    fig, ax = plt.subplots(figsize=(6.6, 3.8))
    kinds = ["habit", "control", "override"]
    for j, mem in enumerate(("on", "off")):
        vals = [next(s for s in summary if s["memory"] == mem and s["kind"] == k) for k in kinds]
        xs = [i + (j - 0.5) * 0.38 for i in range(len(kinds))]
        ax.bar(xs, [v["rate"] for v in vals], 0.38, yerr=[[v["rate"] - v["ci_low"] for v in vals], [v["ci_high"] - v["rate"] for v in vals]],
               capsize=3, label=f"memory {mem}", color="#1f77b4" if mem == "on" else "#c7c7c7")
    ax.set_xticks(range(len(kinds)), ["habit: resolve without asking", "control: must still ask", "override: honour 'standard'"], fontsize=8)
    ax.set_ylim(0, 1.1); ax.set_ylabel("share handled correctly (95% CI)"); ax.legend()
    ax.set_title("C3: context-dependent requests with and without memory")
    name = lib.save(fig, rd, "memory_ablation")
    on = [r for r in recs if r["memory"] == "on"]
    habit_on = [r for r in on if r["kind"] == "habit"]
    habit_off = [r for r in recs if r["memory"] == "off" and r["kind"] == "habit"]
    ok_on = sum(r["correct"] for r in habit_on)
    asked_off = sum(r["asked"] for r in habit_off)
    allc = sum(r["correct"] for r in on)
    verdict = "PASS" if ok_on > len(habit_on) / 2 and all(r["correct"] for r in on if r["kind"] == "control") else "FAIL"
    rd.finish(claim="Confirmed habits let the cloud resolve a repeat request without asking again, without overriding an explicit choice.",
              pass_bar="Most of 20 context-dependent requests resolved without asking (FR-19).",
              verdict=verdict,
              measured=(f"memory on: {allc}/{len(on)} cases right, habit cases resolved without asking {ok_on}/{len(habit_on)}; "
                        f"memory off: the same habit requests asked {asked_off}/{len(habit_off)} times (questions saved by memory)"),
              notes=["The case set repeats three phrasings; labels are drafts until reviewed.",
                     "A habit needs 2 confirmations (cloud/memory.py CONFIRMATIONS); controls with 0 or 1 confirmation must still ask."],
              plots=[name])
    return 0


if __name__ == "__main__":
    sys.exit(main())
