# R9: Store-and-Forward and Reconnection

- **Claim:** Results persist in SQLite over cloud and edge outages and reconcile exactly once.
- **PRD pass bar:** For each 600 s fault: one generated result retained/uploaded once; reconciliation <60 s; normal lease operation <120 s; no conflict.
- **Measured:** R9-A: outage 2.062s, recovery 0.205s, effects 1; R9-B: outage 2.008s, recovery 0.123s, effects 1
- **Verdict:** PASS (SMOKE ONLY; NOT 10-MINUTE ACCEPTANCE EVIDENCE)
- **Source of numbers:** measured (in-process, model n/a)
- **Command:** `python experiments/robot_edge/r9_store_forward_reconnection.py --mode smoke`
- **Commit:** 52730e6-dirty, run 2026-11-07T14:17:01Z to 2026-11-07T14:17:09Z

## Plots

![recovery_time](plots/recovery_time.png)
![result_lifecycle](plots/result_lifecycle.png)

## Notes

- Full mode uses wall-clock 600-second outages for R9-A and R9-B; smoke mode is harness validation only.
- R9-A isolates cloud-edge connectivity via the production OutboxUploadWorker network gate.
- R9-B terminates and restarts a separate EdgeSupervisor process against the same SQLite database.
- The local receiver records cloud processing effects and returns duplicate acknowledgement without a second effect.

Raw per-trial records are in `raw/`; `summary.csv` is computed from them by the same script.
