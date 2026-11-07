# R7: WAN disconnection and lease-expiry parking

- **Claim:** Kinematic R7 records WAN outbox replay and expiry parking.
- **PRD pass bar:** No unsafe park, no post-expiry task motion, buffered WAN events delivered.
- **Measured:** 17/30 trials passed; inspect parking.csv and raw pose/event logs.
- **Verdict:** FAIL
- **Source of numbers:** measured (in-process, model SimRobot kinematic)
- **Command:** `python experiments/robot_edge/r7_safe_parking.py --scenario all --seed 42 --out results/final/R7`
- **Commit:** 52730e6, run 2026-11-07T14:05:22Z to 2026-11-07T14:14:21Z

## Notes

- R7-B currently exposes whether expiry interrupts an active navigation leg; it is not assumed to pass.
- No Gazebo is started.

Raw per-trial records are in `raw/`; `summary.csv` is computed from them by the same script.
