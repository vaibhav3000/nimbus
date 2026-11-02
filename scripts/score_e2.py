"""Score request understanding (PRD E2, FR-13 / FR-17 / FR-19) against the labelled sets in evaluation/e2/.

    python scripts/score_e2.py --config C1 --set requests         # stand-in model by default
    python scripts/score_e2.py --config C3 --set all              # no model: the rules path
    NIMBUS_LLM=bedrock NIMBUS_CASSETTE=data/e2.cassette.json NIMBUS_CASSETTE_MODE=record python scripts/score_e2.py --config C1   # you run (AWS)

What counts as right:
  requests  a plain request: the task's item, destination, source (if labelled), priority and deadline (if labelled) all match; the
            share of correct details is `field_accuracy` (FR-13: >= 85%). A vague request: the system must ASK (clarification
            recall; precision is how many of its questions were warranted).
  rules     the statement becomes the labelled restriction (zone, kind, lift or declare, items to move out) (FR-17: 10 of 10).
  context   after the labelled number of confirmed requests, the bare request is resolved without asking and to the right item;
            controls must still ask; an explicit "standard" must be honoured (FR-19: most resolved without asking).

The labels in the committed sets are DRAFTS until a person reviews them; the report says so.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("NIMBUS_PACK", "hospital_sim")

from cloud.clock import FixedClock  # noqa: E402
from cloud.orchestrator import Orchestrator  # noqa: E402

EVAL = ROOT / "evaluation" / "e2"


def make_orch(config: str) -> Orchestrator:
    previous = os.environ.get("NIMBUS_MODE")
    os.environ["NIMBUS_MODE"] = config
    tmp = tempfile.TemporaryDirectory()          # kept alive with the orchestrator, which writes its state there
    try:
        orch = Orchestrator.from_env(Path(tmp.name), clock=FixedClock())
        orch._tmp = tmp
    finally:
        os.environ.pop("NIMBUS_MODE", None) if previous is None else os.environ.__setitem__("NIMBUS_MODE", previous)
    for cid in orch.fleet.clusters:
        orch.fleet.heartbeat(cid, {})
    return orch


def task(n: int, text: str) -> dict:
    return {"task_id": f"task-{n:06d}", "request_text": text, "status": "received"}


def score_requests(rows: list[dict], predict) -> dict:
    """`predict(text) -> {"asked": bool, "spec": dict|None}`. Returns accuracy numbers (pure function, testable)."""
    per = {"item": [0, 0], "destination": [0, 0], "priority": [0, 0], "deadline_minutes": [0, 0], "source": [0, 0]}
    asked_gold = asked_pred = asked_both = 0
    wrong = []
    for r in rows:
        p, g = predict(r["text"]), r["gold"]
        if p["asked"]:
            asked_pred += 1
        if "ask" in g:
            asked_gold += 1
            asked_both += int(p["asked"])
            if not p["asked"]:
                wrong.append({"id": r["id"], "text": r["text"], "why": "should have asked"})
            continue
        if p["asked"]:
            wrong.append({"id": r["id"], "text": r["text"], "why": "asked although the request was clear"})
            for f in g:
                if f in per:
                    per[f][1] += 1
            continue
        for f in g:
            if f in per:
                per[f][1] += 1
                got = (p["spec"] or {}).get(f)
                if f == "destination":
                    got = (p["spec"] or {}).get("destination_anchor") or got
                ok = got == g[f]
                per[f][0] += int(ok)
                if not ok:
                    wrong.append({"id": r["id"], "text": r["text"], "why": f"{f}: expected {g[f]!r}, got {got!r}"})
    right, total = sum(v[0] for v in per.values()), sum(v[1] for v in per.values())
    return {"field_accuracy": round(right / total, 3) if total else None, "per_field": {k: (round(v[0] / v[1], 3) if v[1] else None) for k, v in per.items()},
            "clarification_recall": round(asked_both / asked_gold, 3) if asked_gold else None,
            "clarification_precision": round(asked_both / asked_pred, 3) if asked_pred else None, "n": len(rows), "wrong": wrong}


def run_requests(config: str, doc: dict) -> dict:
    def predict(text: str) -> dict:
        orch = make_orch(config)
        out = orch.handle_request(task(1, text), text)
        return {"asked": out.kind == "clarify", "spec": out.spec}
    return score_requests(doc["requests"], predict)


def run_rules(config: str, doc: dict) -> dict:
    good, wrong = 0, []
    for r in doc["rules"]:
        orch = make_orch(config)
        if r["gold"]["lift"]:
            orch.policies.declare(r["gold"]["zone"], r["gold"]["kind"])
        out = orch.handle_request(task(1, r["text"]), r["text"])
        zone, active = r["gold"]["zone"], orch.policies.restrictions()
        ok = out.kind == "statement" and ((zone not in active) if r["gold"]["lift"] else (active.get(zone, {}).get("kind") == r["gold"]["kind"]))
        if ok and not r["gold"]["lift"]:
            ok = sorted({c["item"] for c in out.children}) == sorted(r["gold"]["extract_items"]) if r["gold"]["extract_items"] else not out.children
        good += int(ok)
        if not ok:
            wrong.append({"id": r["id"], "text": r["text"], "why": f"got {out.kind}"})
    return {"applied_correctly": good, "n": len(doc["rules"]), "rate": round(good / len(doc["rules"]), 3), "wrong": wrong}


def run_context(config: str, doc: dict) -> dict:
    good, wrong, by_kind = 0, [], {}
    for c in doc["cases"]:
        orch = make_orch(config)
        for k in range(c["setup"]):                                   # build the habit: requests confirmed as paediatric
            t = task(k + 1, "Need an infusion pump at 2B-12, urgent.")
            q = orch.handle_request(t, t["request_text"])
            if q.kind == "clarify":
                o = orch.handle_request(t, t["request_text"], question=q.question, answer="Yes, a paediatric pump please.")
                if o.kind == "leased":
                    orch.finish({"task_id": t["task_id"], "reservation": o.reservation, "spec": o.spec, "lease": o.lease})
        orch.fleet.set_stock("east-wing", {"infusion_pump_paediatric": 9, "infusion_pump": 9})
        out = orch.handle_request(task(50, c["text"]), c["text"])
        g = c["gold"]
        ok = (out.kind == "clarify") if g["ask"] else (out.kind in ("leased", "queued") and (out.spec or {}).get("item") == g["item"])
        by_kind.setdefault(c["kind"], [0, 0]); by_kind[c["kind"]][1] += 1; by_kind[c["kind"]][0] += int(ok)
        good += int(ok)
        if not ok:
            wrong.append({"id": c["id"], "text": c["text"], "kind": c["kind"], "why": f"got {out.kind} {(out.spec or {}).get('item')}"})
    return {"correct": good, "n": len(doc["cases"]), "rate": round(good / len(doc["cases"]), 3),
            "by_kind": {k: f"{v[0]}/{v[1]}" for k, v in by_kind.items()}, "wrong": wrong}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="C1", choices=["C1", "C2", "C3"])
    ap.add_argument("--set", default="all", choices=["requests", "rules", "context", "all"])
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    result = {"config": a.config, "pack": os.environ["NIMBUS_PACK"], "model": os.environ.get("NIMBUS_BEDROCK_MODEL", "stand-in") if os.environ.get("NIMBUS_LLM") == "bedrock" else "stand-in"}
    for name, fn in (("requests", run_requests), ("rules", run_rules), ("context", run_context)):
        if a.set in (name, "all"):
            doc = json.loads((EVAL / f"{name}.json").read_text())
            result[name] = fn(a.config, doc)
            result[name]["label_status"] = doc["status"]
    text = json.dumps(result, indent=1)
    print(text)
    if a.out:
        Path(a.out).write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
