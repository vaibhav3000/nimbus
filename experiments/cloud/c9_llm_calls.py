"""C9 - Cloud model calls and tokens per task, C1 vs C2 vs C3 (input to NFR-4).

The same scripted tasks run against a cloud in each configuration (NIMBUS_MODE=C1|C2|C3), with a simulated edge that
follows that configuration's protocol:
  C1  leased edge autonomy: a detour inside the lease is handled at the edge (no cloud call); only what the lease
      cannot solve is escalated (UNEXPECTED_EVENT)
  C2  thin relay: before every non-trivial step (go_to, wait, photo) the edge asks POST /relay/decide (one model
      call each), and relays the incident the same way
  C3  no model anywhere (the cloud's rules; the edge's rules)
Tasks (PRD Table 3): T1 routine delivery, T2 urgent delivery, T3 vague request + answer, T4 blocked corridor with a
detour inside the lease, T5 gate closed (re-plan needed), T7 inventory survey, T8 three requests at once.
Counted per task from the cloud's own trace (/tasks/{id}/trace, kind=llm): calls by step (intent, allocation,
react, step, result) and input/output tokens. Call COUNTS are protocol behaviour; token sizes are real only on a real
model (with the stand-in they are estimates and labelled so).

    python experiments/cloud/c9_llm_calls.py --target local            # stand-in (counts real, tokens estimated)
    python experiments/cloud/c9_llm_calls.py --target local-bedrock    # real Bedrock tokens (needs AWS credentials)
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402
import scenarios  # noqa: E402

TASKS = {
    "T1": {"text": "Deliver a wheelchair to 2C, routine."},
    "T2": {"text": "Deliver an IV pole to 2B, urgent."},
    "T3": {"text": "a pump to 2B", "answer": "Yes, a paediatric pump please."},
    "T4": {"text": "Deliver a blanket to 2B, routine.", "incident": "local"},
    "T5": {"text": "Deliver a monitor to 2C, routine.", "incident": "escalate"},
    "T7": {"text": "Check how many infusion pumps are idle in 2B and 2C and report back."},
    "T8": {"text": ["Deliver a blanket to 2C, routine.", "Deliver a wheelchair to 2C, routine.", "Deliver an IV pole to 2C, urgent."]},
}
RELAY_CMDS = ("go_to", "wait", "photo")


def make_edge(mode: str, plan: dict):
    def on_lease(edge: lib.Edges, cluster: str, lease: dict):
        tid = edge.task_of(lease)
        sc = plan.get(tid, {})
        steps = lease.get("task_sequence", [])
        for i, s in enumerate(steps):
            if mode == "C2" and s.get("cmd") in RELAY_CMDS:
                edge.t.api.post("/relay/decide", {"cluster_id": cluster, "lease_id": lease["lease_id"], "lease_version": lease["version"],
                                                  "step_index": i, "cmd": s["cmd"], "zone": s.get("params", {}).get("to"),
                                                  "situation": "about to start this step", "message_id": f"exp-relay-{uuid.uuid4().hex[:10]}"})
            last_go = max((k for k, x in enumerate(steps) if x.get("cmd") == "go_to"), default=-1)
            if i == last_go and sc.get("incident") and lease["version"] == 1:
                route = scenarios.delivery_route(lease)
                zone = (route[1:-1] or route)[len(route[1:-1]) // 2] if route else None
                if sc["incident"] == "local":
                    if mode == "C2":    # the thin relay asks the cloud even for a detour the lease allows
                        edge.t.api.post("/relay/decide", {"cluster_id": cluster, "lease_id": lease["lease_id"], "lease_version": lease["version"],
                                                          "step_index": i, "cmd": "go_to", "zone": zone, "situation": f"{zone} is blocked by a trolley",
                                                          "blocked_zones": [zone], "message_id": f"exp-relay-{uuid.uuid4().hex[:10]}"})
                    # C1 and C3: the edge detours inside the lease on its own; nothing is sent
                else:
                    edge.escalate("UNEXPECTED_EVENT", lease, {"what_happened": f"the door at {zone} is closed and locked", "where": zone,
                                                              "robots_saw": [f"{lease['assigned_robots'][0]}: door closed"], "robot_zone": route[0],
                                                              "segments": [{"segment_id": zone, "state": "closed"}]})
                    return None          # wait for the cloud's new version; that one is delivered normally
        return "complete"
    return on_lease


def run_mode(mode: str, spec: str, repeats: int, rd) -> list[dict]:
    target = lib.start_local(mode, bedrock=spec == "local-bedrock")
    plan: dict = {}
    edges = lib.Edges(target, work_s=0.1, on_lease=make_edge(mode, plan)).start()
    recs = []
    try:
        s, pk, _ = target.api.get("/pack")
        children: dict = {}
        for rep in range(1, repeats + 1):
            for cid in edges.robots:               # deliveries consume stock: restock so every repeat can be leased
                target.api.post("/fleet/stock", {"cluster_id": cid, "stock": {i: 1000 for i in pk.get("items", [])}})
            for tname, sc in TASKS.items():
                texts = sc["text"] if isinstance(sc["text"], list) else [sc["text"]]
                tids = []
                for text in texts:
                    st, out, _ = target.request(text)
                    tid = out.get("task_id")
                    if not tid:
                        continue
                    plan[tid] = sc
                    edges.tasks.add(tid)
                    tids.append(tid)
                    if out.get("status") == "needs_clarification" and sc.get("answer"):
                        target.api.post(f"/requests/{tid}/answer", {"text": sc["answer"], "message_id": f"exp-ans-{uuid.uuid4().hex[:8]}"})
                    # a survey (T7) or a multi-item request creates child tasks: the edge must run those leases too
                    for child in (target.api.get(f"/tasks/{tid}")[1].get("children") or []):
                        cid_ = child if isinstance(child, str) else child.get("task_id")
                        if cid_:
                            plan[cid_] = sc
                            edges.tasks.add(cid_)
                            children.setdefault(tid, []).append(cid_)
                for tid in tids:
                    scenarios.wait_for(lambda: target.api.get(f"/tasks/{tid}")[1].get("status") in ("completed", "failed", "cancelled", "rejected", "needs_clarification"), 25, 0.2)
                time.sleep(0.5)
                for tid in tids:
                    s, task, _ = target.api.get(f"/tasks/{tid}")
                    for t_ in [tid] + children.get(tid, []):
                        s_, tk_, _ = target.api.get(f"/tasks/{t_}")
                        if tk_.get("status") not in ("completed", "failed", "cancelled", "rejected"):
                            target.api.post(f"/tasks/{t_}/control", {"action": "cancel", "message_id": f"exp-c-{uuid.uuid4().hex[:8]}"})
                    llm = []
                    for t_ in [tid] + children.get(tid, []):      # a task's cost includes the children it created
                        s, tr, _ = target.api.get(f"/tasks/{t_}/trace")
                        llm += [e["detail"] for e in tr.get("events", []) if e.get("kind") == "llm"]
                    by_step = Counter(d.get("step") for d in llm)
                    rec = {"mode": mode, "repeat": rep, "task": tname, "task_id": tid, "final_status": task.get("status"), "reason": task.get("reason"),
                           "calls": len(llm), "calls_by_step": dict(by_step),
                           "tokens_in": sum(d.get("tokens_in") or 0 for d in llm), "tokens_out": sum(d.get("tokens_out") or 0 for d in llm),
                           "tokens_estimated": any(d.get("estimated") for d in llm), "model": llm[0].get("model") if llm else None,
                           "leased": any(k[0] == tid for k in edges.seen), "children": children.get(tid, []),
                           "lease_versions": max((k[1] for k in edges.seen if k[0] == tid), default=0), "source": target.source}
                    recs.append(rec)
                    rd.raw("tasks", rec)
                s, m, _ = target.api.get("/map.json")      # every task starts from a clear map
                for z in (m.get("closed") or {}):
                    target.api.post("/map/reopen", {"zone": z})
                print(f"[C9] {mode} repeat {rep} {tname}: calls {[r['calls'] for r in recs if r['task'] == tname and r['repeat'] == rep]}")
    finally:
        edges.stop()
        target.close()
    return recs


def main(argv=None) -> int:
    ap = lib.common_args(argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter), "local")
    ap.add_argument("--repeats", type=int, default=3)
    a = ap.parse_args(argv)
    rd = lib.ResultDir("C9" if a.target == "local-bedrock" else "C9_standin", "Cloud model calls per task, C1 vs C2 vs C3 (NFR-4 input)", None)
    rd.reset_raw()
    recs = []
    for mode in ("C1", "C2", "C3"):
        recs += run_mode(mode, a.target, a.repeats, rd)
    rd.env.update({"source": recs[0]["source"] if recs else None, "target": a.target, "model": next((r["model"] for r in recs if r["model"]), None)})
    summary = []
    per_task = defaultdict(dict)
    for mode in ("C1", "C2", "C3"):
        for t in list(TASKS) + ["all"]:
            sub = [r for r in recs if r["mode"] == mode and (t == "all" or r["task"] == t)]
            if not sub:
                continue
            calls = sum(r["calls"] for r in sub) / len(sub)
            per_task[t][mode] = calls
            steps = Counter()
            for r in sub:
                steps.update(r["calls_by_step"])
            summary.append({"mode": mode, "task": t, "tasks_run": len(sub), "calls_per_task": lib.r(calls, 2),
                            "tokens_in_per_task": lib.r(sum(r["tokens_in"] for r in sub) / len(sub), 0),
                            "tokens_out_per_task": lib.r(sum(r["tokens_out"] for r in sub) / len(sub), 0),
                            "tokens_estimated": any(r["tokens_estimated"] for r in sub),
                            **{f"calls_{k}": lib.r(v / len(sub), 2) for k, v in sorted(steps.items())},
                            "completed": sum(r["final_status"] == "completed" for r in sub), "source": sub[0]["source"]})
    rd.summary(summary)
    plt = lib.plt()
    steps = sorted({k for r in recs for k in r["calls_by_step"]})
    fig, ax = plt.subplots(figsize=(8.5, 4))
    tasks = list(TASKS)
    w = 0.27
    palette = ["#1f77b4", "#aec7e8", "#ff7f0e", "#ffbb78", "#2ca02c", "#98df8a", "#9467bd"]
    for j, mode in enumerate(("C1", "C2", "C3")):
        bottom = [0.0] * len(tasks)
        for k, step in enumerate(steps):
            vals = []
            for t in tasks:
                sub = [r for r in recs if r["mode"] == mode and r["task"] == t]
                vals.append(sum(r["calls_by_step"].get(step, 0) for r in sub) / len(sub) if sub else 0)
            xs = [i + (j - 1) * w for i in range(len(tasks))]
            ax.bar(xs, vals, w, bottom=bottom, color=palette[k % len(palette)], edgecolor="white", label=step if j == 0 else None)
            bottom = [b + v for b, v in zip(bottom, vals)]
        for i, b in enumerate(bottom):
            ax.text(i + (j - 1) * w, b + 0.1, mode, ha="center", fontsize=6)
    ax.set_xticks(range(len(tasks)), tasks); ax.set_ylabel("cloud model calls per task")
    ax.set_title("C9: model calls per task by step (left to right C1, C2, C3)"); ax.legend(fontsize=7, title="step")
    name = lib.save(fig, rd, "calls_per_task")
    c1, c2 = per_task["all"].get("C1"), per_task["all"].get("C2")
    ratio = c1 / c2 if c1 is not None and c2 else None
    rd.finish(claim="Leased edge autonomy (C1) needs at most half the cloud model calls of the cloud-only relay (C2).",
              pass_bar="C1 cloud LLM calls per task <= 50% of C2 (NFR-4).",
              verdict="PASS" if ratio is not None and ratio <= 0.5 else "FAIL",
              measured=(f"calls per task C1 {c1:.2f}, C2 {c2:.2f}, C3 {per_task['all'].get('C3', 0):.2f}; C1/C2 = {ratio:.1%}"
                        if ratio is not None else "no data"),
              notes=["Call counts come from the cloud's trace of each task; the simulated edge only decides WHEN to call (per protocol), never what the model answers.",
                     "C2's relay here is the cloud's real POST /relay/decide; the edge supervisor does not call it yet, so this is the C2 protocol as specified, driven by the harness.",
                     "Token counts are estimates unless the run used a real model (see tokens_estimated)."],
              plots=[name])
    return 0


if __name__ == "__main__":
    sys.exit(main())
