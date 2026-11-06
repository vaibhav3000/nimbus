# How to add experiment results

Every final experiment's results live in one folder per experiment under `results/final/`, all in the same format.
`experiments/cloud/report.py` reads those folders and builds one table for the whole project,
[results/final/RESULTS.md](../results/final/RESULTS.md). The README's results section links there. If every folder
follows this guide, nobody has to type numbers by hand, and examiners can trace any number back to its raw records.

## 1. Name your experiment

Use the experiment IDs from the plan:

| IDs | Area |
| --- | --- |
| C1 to C9 | Cloud |
| R1 to R10 | Robot and edge |
| N1 to N10 | Network and infrastructure |

If you run the same experiment in more than one setting, add a short suffix: `R2_before`, `R2_after`,
`N3_basement`, `C1_deployed`. The table sorts by group (C, R, N) and then by number.

## 2. Put the code in the right place

| Area | Code folder |
| --- | --- |
| Robot and edge | `experiments/robot_edge/` |
| Network and infrastructure | `experiments/network_infra/` |
| Cloud | `experiments/cloud/` |

One script per experiment, named after its ID, for example `experiments/robot_edge/r1_safety_stop.py`. Put the
command that reproduces your result at the top of the script's docstring.

## 3. Write the results folder

`results/final/<ID>/` must contain:

| Item | What goes in it |
| --- | --- |
| `raw/` | One JSON object per line (`.jsonl`), one line per trial or measurement, exactly as measured. Compress files over about 10 MB with gzip (`.jsonl.gz`). Videos and screen recordings go here too, or a link to them in `README.md`. |
| `summary.csv` | One row per condition (e.g. per speed, per model, per trace), computed from `raw/` by your script. It must have a `source` column (see below). |
| `plots/` | Each plot as `.png` (300 dpi) and `.svg`. Units on every axis. Draw the plan's pass bar as a dashed red line where there is one. |
| `env.json` | Where and how it ran (see below). |
| `README.md` | The result in the fixed format below. `report.py` reads these lines, so keep the field names exactly. |

### README.md format

```markdown
# R1: Safety stop with the network cut

- **Claim:** A robot stops before an obstacle without edge or cloud.
- **PRD pass bar:** 30 trials with the network cut: every robot stops (FR-1).
- **Measured:** 30/30 stopped; stop latency p95 0.21 s; minimum clearance 0.27 m (95% CI 0.25 to 0.29 m)
- **Verdict:** PASS
- **Source of numbers:** measured (Gazebo, 6 robots)
- **Command:** `python experiments/robot_edge/r1_safety_stop.py --trials 30`
- **Commit:** abc1234, run 2026-11-08T10:00:00Z to 2026-11-08T10:40:00Z

## Plots

![stop_latency_cdf](plots/stop_latency_cdf.png)

## Notes

- Anything a reader must know to interpret the numbers: limitations, what was simulated, what is still missing.
```

- **Verdict:** `PASS` or `FAIL` against the plan's pass bar. Do not soften it: a FAIL with a clear note is more useful
  than a PASS nobody can check.
- **Measured:** the headline number with its uncertainty. Use the median with a 95% bootstrap interval for times,
  and the count with a Wilson 95% interval for rates. The helpers are `bootstrap_ci` and `wilson` in
  `experiments/cloud/lib.py`.

### env.json fields

```json
{
  "experiment": "R1",
  "title": "Safety stop with the network cut",
  "prd": "FR-1",
  "source": "measured",
  "target": "gazebo, 6 robots, Docker image kautuk05/ds252-icc:gazebo_hospital",
  "model": "n/a",
  "git_commit": "abc1234",
  "started_utc": "2026-11-08T10:00:00Z",
  "finished_utc": "2026-11-08T10:40:00Z",
  "host": "...",
  "command": "python experiments/robot_edge/r1_safety_stop.py --trials 30"
}
```

`prd` is what appears in the table's PRD column, so always set it. `source` must be one of:

- `measured`: real measurement, no language model involved (Gazebo, hardware, network traces, audits)
- `real_bedrock`: Amazon Bedrock was the model
- `real_slm`: a real edge model (Ollama or llama.cpp; name it in `model`)
- `mock_llm`: a stand-in model (shows that the code and protocol work, not model quality)
- `modelled`: computed from assumed constants rather than measured; avoid, and say why in the notes if you must

## 4. The easy way: use `ResultDir`

`experiments/cloud/lib.py` writes all of the above for you:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cloud"))
import lib

rd = lib.ResultDir("R1", "Safety stop with the network cut", None, prd="FR-1")
rd.env.update({"source": "measured", "target": "gazebo, 6 robots", "model": "n/a"})
rd.reset_raw()
for trial in trials:
    rd.raw("trials", {"trial": trial.id, "speed": trial.speed, "stopped": trial.stopped, "clearance_m": trial.clearance})
rd.summary(rows)                       # list of dicts, one per condition, each with "source"
plt = lib.plt()                        # the shared plot style
fig, ax = plt.subplots()
...
lib.pass_line(ax, 0.20, "FR-1: clearance at least 0.20 m")
name = lib.save(fig, rd, "clearance_by_speed")
rd.finish(claim="...", pass_bar="...", verdict="PASS" if ok else "FAIL", measured="...", notes=[...], plots=[name])
```

## 5. Update the docs

1. Rebuild the table: `python experiments/cloud/report.py`.
2. In `README.md`, section 7, replace your experiment's "to be filled" row with one plain sentence of what it showed,
   with the key number. Link the folder, e.g. `[R1](results/final/R1/README.md)`.
3. If the experiment changed code (a fix it found), add a regression test and one line in `dev.md` (section 13,
   Known issues, if something remains open).

## 6. Push

```bash
git pull --rebase origin main
```

```bash
git add experiments/<area>/ results/final/<ID>/ results/final/RESULTS.md README.md
```

```bash
git commit -m "<ID>: <one line of what it showed>"
```

```bash
git push origin main
```

## Rules

- **No hand-typed numbers.** Every number in `summary.csv`, the README and the main README comes from the run's raw
  records, through your script.
- **Rerunnable.** Seeds are fixed and written down; the command in the README reproduces the folder.
- **Label every number.** Every number says where it came from (`source`). A stand-in or modelled number is never
  presented as real.
- **Leave no trace on the shared deployment.** By default, run against a local cloud (`--target local` or
  `local-bedrock`). If an experiment must use the deployed cloud, cancel every task it created, restore any stock it
  used, and make sure no one else's edges are connected while it runs.
- **No secrets** (keys, tokens) in code, logs or results, and no real personal data. Datasets must be listed in
  `evaluation/manifest.json`.
