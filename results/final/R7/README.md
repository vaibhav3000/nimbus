# R7: WAN disconnection and lease-expiry parking

- **Claim:** Kinematic R7 records WAN outbox replay and expiry parking.
- **PRD pass bar:** No unsafe park, no post-expiry task motion, buffered WAN events delivered.
- **Measured:** 30/30 seeded trials passed: R7-A delivered the buffered WAN event and completed navigation in 20/20 link-cut trials; R7-B safely parked in `east_waiting_approach` in 10/10 expiry trials (1.477–2.335 s). There were 0 post-expiry task-motion events.
- **Verdict:** PASS
- **Source of numbers:** measured (in-process, model SimRobot kinematic)
- **Command:** `python experiments/robot_edge/r7_safe_parking.py --scenario all --seed 42 --out results/final/R7`
- **Commit:** 81d3b18-dirty, run 2026-11-07T15:17:34Z to 2026-11-07T15:25:43Z

## Notes

- R7-A does not initiate a park: it verifies that navigation completes and the buffered event is delivered after reconnection. Its blank `parking_zone`/`parking_safe=False` fields mean “no park requested,” not an unsafe park.
- R7-B interrupts active navigation on lease expiry and selects the safe parking zone; all 10 records have `parking_safe=True`.
- No Gazebo is started.

Raw per-trial records are in `raw/`; `summary.csv` is computed from them by the same script.
