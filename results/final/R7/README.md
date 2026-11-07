# R7: WAN disconnection and lease-expiry parking

- **Claim:** Kinematic R7 records WAN outbox replay and expiry parking.
- **PRD pass bar:** No unsafe park, no post-expiry task motion, buffered WAN events delivered.
- **Measured:** 0/30 trials passed; inspect parking.csv and raw pose/event logs.
- **Verdict:** FAIL
- **Source of numbers:** measured (in-process, model SimRobot kinematic)
- **Command:** `python experiments/robot_edge/r7_safe_parking.py --scenario all --seed 42 --out results/final/R7`
- **Commit:** c607884-dirty, run 2026-11-07T13:53:58Z to 2026-11-07T13:54:41Z

## Notes

- R7-B currently exposes whether expiry interrupts an active navigation leg; it is not assumed to pass.
- No Gazebo is started.

Raw per-trial records are in `raw/`; `summary.csv` is computed from them by the same script.
