"""Collect every experiment folder in results/final/ into one table: results/final/RESULTS.md.

Reads each folder's README.md (claim, pass bar, measured, verdict) and env.json (source, target, model, commit), so the
table is always rebuilt from what the experiments wrote, never typed by hand.

    python experiments/cloud/report.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

GROUP_ORDER = {"C": 0, "R": 1, "N": 2, "E": 3}
ORDER = ["C1", "C1_deployed", "C1_standin", "C2", "C2_standin", "C3", "C3_standin", "C4", "C5", "C6", "C6_local",
         "C7", "C7_local", "C8", "C8_local", "C9", "C9_standin"]
PRD = {"C1": "FR-13", "C2": "FR-17", "C3": "FR-19", "C4": "FR-15", "C5": "FR-14, T8", "C6": "FR-16, T5", "C7": "FR-18",
       "C8": "NFR-1 (cloud)", "C9": "NFR-4"}


def field(text: str, name: str) -> str:
    m = re.search(rf"\*\*{re.escape(name)}:\*\* (.+)", text)
    return m.group(1).strip() if m else ""


def main() -> int:
    rows = []
    def key(d):
        num = re.match(r"([A-Z])(\d+)", d.name)
        return (GROUP_ORDER.get(d.name[0], 9), int(num.group(2)) if num else 99, ORDER.index(d.name) if d.name in ORDER else 99, d.name)
    dirs = sorted((d for d in lib.RESULTS.iterdir() if d.is_dir() and (d / "README.md").exists()), key=key)
    for d in dirs:
        text = (d / "README.md").read_text()
        env = json.loads((d / "env.json").read_text()) if (d / "env.json").exists() else {}
        exp = d.name.split("_")[0]
        rows.append({"run": d.name, "prd": env.get("prd") or PRD.get(exp, ""), "verdict": field(text, "Verdict"), "pass_bar": field(text, "PRD pass bar"),
                     "measured": field(text, "Measured"), "source": env.get("source", ""), "target": env.get("target", ""),
                     "model": env.get("model", ""), "commit": env.get("git_commit", ""), "when": env.get("finished_utc", "")})
    lines = ["# Experiment results", "",
             "Built by `python experiments/cloud/report.py` from each folder's README.md and env.json. "
             "Every row links to a folder with the raw records, summary, plots and environment. How to add a row: docs/RESULTS_GUIDE.md. "
             "Source: `real_bedrock` = Amazon Bedrock Nova Lite (deployed cloud or our own local server), "
             "`mock_llm` = the rule-based stand-in model (protocol and code behaviour, not model quality), "
             "`measured` = no model involved (or a physical/simulated measurement).", "",
             "| Run | PRD | Verdict | Measured | Source | Target | Commit |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for r in rows:
        lines.append(f"| [{r['run']}]({r['run']}/README.md) | {r['prd']} | {r['verdict']} | {r['measured'].replace('|', '/')} | "
                     f"{r['source']} | {r['target']} | {r['commit']} |")
    lines += ["", "Pass bars:", ""] + [f"- **{r['run']}**: {r['pass_bar']}" for r in rows]
    (lib.RESULTS / "RESULTS.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines[:len(rows) + 6]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
