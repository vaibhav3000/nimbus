"""Turn results/e3/results.json into docs/e3-results.md (tables for every task and configuration, the NFR-4 verdict, and links to
the plots). Everything labelled "modelled" comes from constants in scripts/run_e3.py; calls, tokens, bytes and success are measured.

    python scripts/e3_report.py [--in results/e3/results.json] [--out docs/e3-results.md]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TITLES = {"T1": "Routine delivery", "T2": "Urgent under background traffic", "T3": "Vague request", "T4": "Blocked corridor, detour inside the lease",
          "T5": "Gate closed: cross-wing hand-over", "T6": "Ward declared infectious mid-mission", "T7": "Inventory sweep (survey)",
          "T8": "Three requests, two robots"}


def render(doc: dict) -> str:
    s = doc["summary"]
    lines = ["# E3 results: C1 vs C2 vs C3 on T1-T8", "",
             f"Map `{doc['pack']}`, model **{doc['model']}**, {doc['seeds']} seeded runs per cell (medians). "
             "Success, model calls, tokens and WAN bytes are measured from the real code; completion time and latency are MODELLED "
             "(constants in `scripts/run_e3.py`: model call 2.5 s, round trip 80 ms, edge decision 0.5 s). "
             + ("**The model here is the offline stand-in, so token counts are estimates and no model-quality conclusion should be drawn.**"
                if doc["model"] == "stand-in" else "Model calls come from a recorded Bedrock run."), "",
             "| Task | Config | Success | Deadline met | Model calls | WAN bytes | Cost / task (USD) | Modelled completion (s) |", "|---|---|---|---|---|---|---|---|"]
    for t, title in TITLES.items():
        for c in ("C1", "C2", "C3"):
            r = s.get(f"{t}/{c}")
            if r:
                lines.append(f"| {t} {title if c == 'C1' else ''} | {c} | {r['success_rate']:.0%} | {r['deadline_rate']:.0%} | {r['llm_calls_median']:.0f} | "
                             f"{r['wan_bytes_median']:.0f} | {r['cost_usd_median']:.2e} | {r['modelled_completion_s_median']:.0f} |")
    n = s["NFR-4"]
    lines += ["", f"**NFR-4** (C1's model calls per task at most 50% of C2's): C1 {n['c1_llm_calls']} vs C2 {n['c2_llm_calls']}, ratio "
              f"**{n['ratio_c1_over_c2']}** ({'met' if n['met'] else 'not met'}).", "",
              "Notes: T7's WAN bytes differ because only C1 has an edge model to filter what the robots saw (the PRD expects C1 to use the least "
              "bandwidth); C2 and C3 ship every photo's result. T6's cloud decisions are the same in C1 and C2 because the edge is not involved "
              "until the recall reaches it.", "", "## Plots", ""]
    lines += [f"![{n}](../results/e3/{n}.svg)" for n in ("success", "llm_calls", "cost", "wan_bytes", "completion", "tradeoff")]
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--in", dest="src", default=str(ROOT / "results" / "e3" / "results.json"))
    ap.add_argument("--out", default=str(ROOT / "docs" / "e3-results.md"))
    a = ap.parse_args()
    Path(a.out).write_text(render(json.loads(Path(a.src).read_text())), encoding="utf-8")
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
