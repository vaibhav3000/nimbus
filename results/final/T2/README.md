# T2: Urgent delivery under background traffic

- **Claim:** A single local animated pedestrian follows a deterministic south-to-north east-corridor lane for T2.
- **PRD pass bar:** Actor SDF is valid; route is free of static obstacles and stays at least 0.50 m from the robot east-corridor centerline.
- **Measured:** start (4.0, -13.0), end (4.0, 13.0), 0.6 m/s for 43.3 s; static clearance 0.50 m; robot-centerline clearance 0.50 m. Smoke result: scenario generated and SDF validated; robot mission was not commanded.
- **Verdict:** PASS
- **Source of numbers:** measured (in-process, model Gazebo Fortress actor preflight)
- **Command:** `python experiments/robot_edge/t2_background_traffic.py --seed 1 --out results/final/T2`
- **Commit:** 42d5a5d-dirty, run 2026-11-07T13:05:44Z to 2026-11-07T13:05:45Z

## Notes

- The source hospital_six_racecars.sdf remains unchanged; the actor exists only in raw/t2_background_traffic.sdf.
- The actor uses the installed local /usr/share/gazebo-11/media/models/walk.dae; no runtime download is used.
- This actor has no collision geometry, so it is visual/dynamic background traffic and cannot physically block the planned robot route.

Raw per-trial records are in `raw/`; `summary.csv` is computed from them by the same script.
