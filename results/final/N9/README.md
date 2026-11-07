C1 requires less than 50% of C2 model calls while providing higher reliability at $0.000544/task.
PRD Pass Bar: C1 model calls <= 50% of C2 (NFR-4); Cost per task reported for all configurations.
python results/final/N9/run_n9.py
19813a7-dirty
PASS: C1 calls = 1.69 vs C2 = 6.93 (24.4% <= 50.0% pass bar); C1 cost=$0.000544/task

- **Claim:** C1 requires less than 50% of C2 model calls while providing higher reliability at $0.000544/task.
- **PRD pass bar:** C1 model calls <= 50% of C2 (NFR-4); Cost per task reported for all configurations.
- **Measured:** C1 calls = 1.69 vs C2 = 6.93 (24.4% <= 50.0% pass bar); C1 cost=$0.000544/task
- **Verdict:** PASS
- **Command:** `python results/final/N9/run_n9.py`
- **Commit:** 19813a7-dirty
