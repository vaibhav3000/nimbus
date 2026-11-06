"""C1 - Request understanding (PRD E2 cloud part, FR-13).

Sends every labelled request to the cloud's public POST /requests and reads back what the cloud made of it:
  asked   -> the task is `needs_clarification` (the cloud asked the requester a question)
  spec    -> GET /tasks/{id} `spec` (item, destination, priority, deadline_minutes) for everything else
and scores it against the gold labels in evaluation/e2/requests.json (50, the PRD set) and
evaluation/e2/requests_paraphrase.json (50 held-out paraphrases).

Deciders (one per --decider):
  bedrock    Amazon Bedrock Nova Lite: a local server (--target local-bedrock, default) or the deployed cloud
             (--target deployed)                                   source real_bedrock
  standin    a local server with the rule-based stand-in model     source mock_llm
  rules      a local server in C3 (no model at all)               source measured
  bedrock2   a local server on a second Bedrock model (--model)    source real_bedrock (needs AWS credentials)

Scoring (FR-13): a clear request is right field by field (item, destination, priority, deadline_minutes);
a vague one is right if the cloud asked. field_accuracy = right fields / labelled fields of clear requests that were
NOT wrongly turned into a question (those count every field wrong). Clarification recall = vague requests asked /
vague requests; precision = vague requests asked / all questions asked.

    python experiments/p3/c1_understanding.py                          # all deciders that need no AWS keys
    python experiments/p3/c1_understanding.py --decider bedrock2 --model apac.anthropic.claude-3-haiku-20240307-v1:0
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

FIELDS = ("item", "destination", "priority", "deadline_minutes")
SETS = {"prd": "requests.json", "paraphrase": "requests_paraphrase.json"}


def load_sets() -> list[dict]:
    rows = []
    for name, fn in SETS.items():
        doc = json.loads((lib.ROOT / "evaluation" / "e2" / fn).read_text())
        rows += [{**r, "set": name} for r in doc["requests"]]
    return rows


def ask_cloud(target: lib.Target, text: str) -> dict:
    st, out, dt = target.request(text)
    rec = {"http": st, "latency_s": round(dt, 3), "task_id": out.get("task_id"), "status": out.get("status"),
           "question": out.get("question"), "error": out.get("error")}
    if out.get("task_id"):
        st2, task, _ = target.api.get(f"/tasks/{out['task_id']}")
        spec = task.get("spec") or {}
        rec["spec"] = {"item": spec.get("item"), "destination": spec.get("destination_anchor") or spec.get("destination"),
                       "priority": spec.get("priority"), "deadline_minutes": spec.get("deadline_minutes")}
        rec["status"] = task.get("status", rec["status"])
        st3, tl, _ = target.api.get(f"/tasks/{out['task_id']}/trace")
        calls = [e["detail"] for e in tl.get("events", []) if e.get("kind") == "llm"]
        rec["llm_calls"] = len(calls)
        rec["llm_ms"] = [c.get("ms") for c in calls]
        rec["tokens_in"] = sum(c.get("tokens_in", 0) or 0 for c in calls)
        rec["tokens_out"] = sum(c.get("tokens_out", 0) or 0 for c in calls)
        rec["model"] = calls[0].get("model") if calls else None
    rec["asked"] = rec["status"] == "needs_clarification"
    return rec


def score(row: dict, rec: dict) -> dict:
    gold = row["gold"]
    out = {"vague": "ask" in gold, "asked": rec["asked"], "fields": {}}
    if "ask" in gold:
        out["correct"] = rec["asked"]
        return out
    for f in FIELDS:
        got = None if rec["asked"] else (rec.get("spec") or {}).get(f)
        out["fields"][f] = {"gold": gold[f], "got": got, "ok": got == gold[f]}
    out["correct"] = all(v["ok"] for v in out["fields"].values())
    return out


def run_decider(decider: str, a, rd: lib.ResultDir, rows: list[dict]) -> list[dict]:
    if decider == "bedrock":
        target = lib.open_target(a.target)       # local-bedrock (own Nova Lite) by default; or the deployed URL
    elif decider == "standin":
        target = lib.start_local("C1")
    elif decider == "rules":
        target = lib.start_local("C3")
    elif decider == "bedrock2":
        target = lib.start_local("C1", bedrock=True, extra_env={"NIMBUS_BEDROCK_MODEL": a.model})
    else:
        raise SystemExit(f"unknown decider {decider}")
    info = target.info()
    src = "measured" if decider == "rules" else target.source
    print(f"[C1] {decider}: {info.get('target')} model={info.get('model')} mode={info.get('mode')}")
    repeats = a.repeats if decider in ("bedrock", "bedrock2") else 1      # local stand-in and rules are deterministic
    recs = []
    try:
        for rep in range(1, repeats + 1):
            for i, row in enumerate(rows):
                rec = ask_cloud(target, row["text"])
                s = score(row, rec)
                full = {"decider": decider, "source": src, "model": info.get("model") if decider != "rules" else None,
                        "repeat": rep, "set": row["set"], "id": row["id"], "text": row["text"], "gold": row["gold"], **rec, **s}
                rd.raw(f"requests_{decider}", full)
                recs.append(full)
                if (i + 1) % 25 == 0:
                    print(f"[C1] {decider} repeat {rep}: {i + 1}/{len(rows)}")
                if decider in ("bedrock", "bedrock2"):
                    time.sleep(a.pause)
    finally:
        target.close()
    return recs


def aggregate(recs: list[dict]) -> dict:
    clear = [r for r in recs if not r["vague"]]
    vague = [r for r in recs if r["vague"]]
    per = {}
    for f in FIELDS:
        k = sum(r["fields"][f]["ok"] for r in clear)
        per[f] = (k, len(clear))
    right, total = sum(v[0] for v in per.values()), sum(v[1] for v in per.values())
    asked = [r for r in recs if r["asked"]]
    va = sum(r["asked"] for r in vague)
    return {"n": len(recs), "n_clear": len(clear), "n_vague": len(vague),
            "field_accuracy": right / total if total else None, "field_ci": lib.wilson(right, total),
            "per_field": {f: (k / n if n else None, lib.wilson(k, n)) for f, (k, n) in per.items()},
            "request_accuracy": sum(r["correct"] for r in recs) / len(recs) if recs else None,
            "clar_recall": va / len(vague) if vague else None, "clar_recall_ci": lib.wilson(va, len(vague)),
            "clar_precision": va / len(asked) if asked else None, "clar_precision_ci": lib.wilson(va, len(asked)),
            "false_questions": sum(1 for r in clear if r["asked"]),
            "confusion": {"vague_asked": va, "vague_not_asked": len(vague) - va,
                          "clear_asked": sum(r["asked"] for r in clear), "clear_not_asked": sum(not r["asked"] for r in clear)}}


def consistency(recs: list[dict]) -> float | None:
    """Share of requests whose outcome (asked + spec) is identical in every repeat (model nondeterminism check)."""
    by = defaultdict(list)
    for r in recs:
        by[(r["set"], r["id"])].append(json.dumps([r["asked"], r.get("spec")], sort_keys=True))
    multi = [v for v in by.values() if len(v) > 1]
    return sum(len(set(v)) == 1 for v in multi) / len(multi) if multi else None


def plots(rd, aggs: dict, recs_by: dict) -> list[str]:
    plt = lib.plt()
    names, out = list(aggs), []
    # 1. field accuracy grouped bars with Wilson CI
    fig, ax = plt.subplots(figsize=(7.2, 4))
    labels = list(FIELDS) + ["all fields"]
    w = 0.8 / max(1, len(names))
    for j, d in enumerate(names):
        vals, lo, hi = [], [], []
        for f in FIELDS:
            v, (l, h) = aggs[d]["per_field"][f]
            vals.append(v or 0); lo.append((v or 0) - (l or 0)); hi.append((h or 0) - (v or 0))
        v, (l, h) = aggs[d]["field_accuracy"], aggs[d]["field_ci"]
        vals.append(v or 0); lo.append((v or 0) - (l or 0)); hi.append((h or 0) - (v or 0))
        xs = [i + (j - (len(names) - 1) / 2) * w for i in range(len(labels))]
        ax.bar(xs, vals, w, yerr=[lo, hi], capsize=2, label=d, color=["#1f77b4", "#7f7f7f", "#bcbd22", "#9467bd"][j % 4])
    lib.pass_line(ax, 0.85, "FR-13 pass bar (85%)")
    ax.set_xticks(range(len(labels)), labels)
    ax.set_ylim(0, 1.05); ax.set_ylabel("share of fields correct (95% Wilson CI)")
    ax.set_title("C1: request details extracted correctly, by decider")
    ax.legend(loc="lower left", fontsize=8)
    out.append(lib.save(fig, rd, "field_accuracy"))
    # 2. clarification confusion matrices
    fig, axes = plt.subplots(1, len(names), figsize=(3.2 * len(names), 3.2), squeeze=False)
    for ax, d in zip(axes[0], names):
        c = aggs[d]["confusion"]
        m = [[c["vague_asked"], c["vague_not_asked"]], [c["clear_asked"], c["clear_not_asked"]]]
        ax.imshow(m, cmap="Blues")
        for i in range(2):
            for k in range(2):
                ax.text(k, i, m[i][k], ha="center", va="center", fontsize=12)
        ax.set_xticks([0, 1], ["asked", "did not ask"]); ax.set_yticks([0, 1], ["vague", "clear"])
        ax.set_title(f"{d}\nrecall {aggs[d]['clar_recall']:.2f}, precision {(aggs[d]['clar_precision'] or 0):.2f}", fontsize=9)
        ax.grid(False)
    fig.suptitle("C1: did the cloud ask when it should?")
    out.append(lib.save(fig, rd, "clarification_confusion"))
    # 3. decision latency on the real model
    if "bedrock" in recs_by:
        fig, ax = plt.subplots()
        for kind, sel in (("asked a question", lambda r: r["asked"]), ("made a task spec", lambda r: not r["asked"])):
            lib.ecdf(ax, [r["latency_s"] for r in recs_by["bedrock"] if sel(r)], kind)
        ax.set_xlabel("request to cloud decision (s), deployed cloud, real Bedrock"); ax.set_ylabel("share of requests")
        ax.set_title("C1: time for the cloud to understand a request"); ax.legend()
        out.append(lib.save(fig, rd, "decision_latency_cdf"))
    return out


def main(argv=None) -> int:
    ap = lib.common_args(argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter))
    ap.add_argument("--decider", action="append", choices=["bedrock", "standin", "rules", "bedrock2"],
                    help="repeatable; default: bedrock standin rules")
    ap.add_argument("--repeats", type=int, default=3, help="repeats per request for real-model deciders")
    ap.add_argument("--model", default="apac.anthropic.claude-3-haiku-20240307-v1:0", help="model for bedrock2")
    ap.add_argument("--pause", type=float, default=0.2, help="seconds between requests to the real model")
    ap.add_argument("--limit", type=int, default=0, help="only the first N requests (smoke test)")
    a = ap.parse_args(argv)
    deciders = a.decider or ["bedrock", "standin", "rules"]
    rows = load_sets()
    if a.limit:
        rows = rows[:a.limit]
    rid = ("C1_deployed" if a.target not in ("local", "local-bedrock") else "C1") if "bedrock" in deciders or "bedrock2" in deciders else "C1_standin"
    info_target = lib.open_target(a.target) if "bedrock" in deciders else None
    rd = lib.ResultDir(rid, "Request understanding (FR-13, E2 cloud)", info_target)
    if info_target is not None and not info_target.is_deployed:
        info_target.close()
    rd.reset_raw()
    recs_by, aggs, summary = {}, {}, []
    for d in deciders:
        recs_by[d] = run_decider(d, a, rd, rows)
        aggs[d] = ag = aggregate(recs_by[d])
        for set_name in ("all",) + tuple(SETS):
            sub = recs_by[d] if set_name == "all" else [r for r in recs_by[d] if r["set"] == set_name]
            if not sub:
                continue
            g = aggregate(sub)
            lat = [r["latency_s"] for r in sub]
            summary.append({"decider": d, "set": set_name, "source": sub[0]["source"], "model": sub[0].get("model"),
                            "requests": g["n"], "clear": g["n_clear"], "vague": g["n_vague"],
                            "field_accuracy": lib.r(g["field_accuracy"]), "field_ci_low": lib.r(g["field_ci"][0]), "field_ci_high": lib.r(g["field_ci"][1]),
                            **{f"acc_{f}": lib.r(g["per_field"][f][0]) for f in FIELDS},
                            "request_accuracy": lib.r(g["request_accuracy"]),
                            "clarification_recall": lib.r(g["clar_recall"]), "clarification_precision": lib.r(g["clar_precision"]),
                            "questions_on_clear_requests": g["false_questions"],
                            "latency_p50_s": lib.r(lib.percentile(lat, 50)), "latency_p95_s": lib.r(lib.percentile(lat, 95)),
                            "repeat_consistency": lib.r(consistency(sub))})
    rd.summary(summary)
    errs = Counter()
    examples = []
    for d, recs in recs_by.items():
        for r in recs:
            if r["repeat"] != 1 or r["correct"]:
                continue
            why = ("should have asked" if r["vague"] else "asked although clear" if r["asked"] else
                   "; ".join(f"{f}: {v['got']!r} not {v['gold']!r}" for f, v in r["fields"].items() if not v["ok"]))
            errs[(d, why.split(":")[0])] += 1
            examples.append({"decider": d, "id": r["id"], "text": r["text"], "why": why})
    rd.json("errors.json", {"counts": {f"{d} | {w}": n for (d, w), n in errs.most_common()}, "examples": examples})
    names = plots(rd, aggs, recs_by)
    main_d = "bedrock" if "bedrock" in aggs else deciders[0]
    m = aggs[main_d]
    ok = (m["field_accuracy"] or 0) >= 0.85 and (m["clar_recall"] or 0) > 0.5
    rd.finish(claim="The cloud turns plain-language requests into structured tasks and asks when a request is vague.",
              pass_bar=">= 85% of details right on clear requests; most vague requests get a question (FR-13).",
              verdict="PASS" if ok else "FAIL",
              measured=(f"{main_d}: field accuracy {m['field_accuracy']:.1%} (95% CI {m['field_ci'][0]:.1%} to {m['field_ci'][1]:.1%}), "
                        f"clarification recall {m['clar_recall']:.1%}, precision {(m['clar_precision'] or 0):.1%}, "
                        f"{m['n']} request decisions"),
              notes=[f"{d}: field accuracy {aggs[d]['field_accuracy']:.1%}, recall {aggs[d]['clar_recall']:.1%}, "
                     f"precision {(aggs[d]['clar_precision'] or 0):.1%}, {aggs[d]['false_questions']} questions on clear requests"
                     for d in aggs] +
                    ["Labels in both request sets are drafts written by the team; review them before quoting as gold.",
                     "A deadline that the request does not state is scored against the pack default (urgent 12 min, routine 60 min).",
                     "On the deployed cloud, earlier confirmed habits (FR-19 memory) can legitimately resolve a vague pump request "
                     "without a question; such cases count as 'should have asked' here. errors.json lists every miss."],
              plots=names)
    return 0


if __name__ == "__main__":
    sys.exit(main())
