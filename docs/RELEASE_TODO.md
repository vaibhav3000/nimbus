# Nimbus — Release Readiness TODO

Derived from the 2026-11-06 code review of `/root/nimbus_final` @ `f3b478a` (container `peaceful_leakey`)
against **PRD v5.0** (`prd.pdf`, 26 Sep 2026).

Full triage report: https://claude.ai/artifact/DhCTg4VpVMs5shAKyE5r3H

**Framing:** the system is in better shape than the results are. 517/518 tests pass, the lease
protocol is real, the deployed cloud genuinely runs Bedrock `apac.amazon.nova-lite-v1:0`. Almost
nothing below is new feature work — it is regenerating, retracting, or correctly labelling results,
plus two real bug fixes.

Effort totals: **P0 ≈ 1h30m · P1 ≈ 4h · P2 ≈ 3h · P3 optional**

---

## Phase 0 — Preserve before anything else

Nothing here is optional and nothing here takes longer than ten minutes.

- [ ] **0.1 — Back up `data/` off the container.**
  `run_all.sh:55` runs `rm -rf "$DATA_DIR" "$ROOT"/edge_*.db` on *every* launch unless `KEEP_DATA=1`.
  That directory currently holds `decisions.jsonl`, `claims.json`, `tasks.json`, `outbox.jsonl` and
  both edge SQLite stores — the FR-20 decision-log evidence from the last run.
  ```bash
  docker cp peaceful_leakey:/root/nimbus_final/data ./nimbus_data_backup_20261106
  ```
  *Acceptance:* backup exists on the host. **~2 min.**

- [ ] **0.2 — Commit and push the 12 uncommitted files.** *(deferred — do in the git pass)*
  They are not scratch work. `edge/supervisor/geofence.py` closes an **FR-5 containment hole**:
  it fences `via` waypoints, which leases actually carry (`data/claims.json` shows
  `"via": ["south_equipment_approach", "east_south_hall_corner", ...]`). Without it, per the
  code's own comment, *"a valid destination [can] take an invalid corridor."*
  `cloud/leasebuilder.py` gives the pickup leg a checked route; `edge/supervisor/supervisor.py`
  canonicalises waypoints so the sim can't fall back to unconstrained planning.
  **The code on `origin/main` is weaker than the code that passes 517 tests locally.**
  *Acceptance:* `git status` clean, `origin/main` moved. **~20 min including review.**

- [ ] **0.3 — Tag the submission commit.** No tags exist. Given the PRD's M1/M2/M3 structure,
  `git tag -a submission-m3 -m "6 Nov submission"` makes "what did we submit" answerable later.
  **~2 min.**

---

## Phase 1 — Integrity blockers

> These must be done before the repo is submitted or demoed. Each one is a claim the repository
> cannot support.

- [ ] **1.1 — Quarantine the four unreproducible result files.**
  No code in the repo produces any of these. `run_e4.py` fabricates its dataset from constants;
  there is no `run_e5.py` at all; the benchmark scripts write `.md`, not these CSVs.

  | File | Problem |
  |---|---|
  | `results/e4.csv`, `P2_Results/e4.csv` | Generated from hardcoded constants; regenerates byte-identically with the whole stack down |
  | `results/plots/e4_recovery_plot.svg`, `P2_Results/plots/…` | Plots of the above |
  | `results/e5_edge.csv`, `P2_Results/e5_edge.csv` | No generator exists anywhere |
  | `results/e1_models.csv`, `P2_Results/e1_models.csv` | No generator; contradicts `e1_results.md` (see 2.5) |
  | `results/e2_edge.csv`, `P2_Results/e2_edge.csv` | No generator |

  Use `git mv` into `archive/unverified_results/` rather than deleting — they are tracked and
  already pushed, so history keeps them regardless.
  *Acceptance:* nothing in `results/` or `P2_Results/` lacks a runnable generator.
  *Violates:* NFR-6. **~15 min.**

- [ ] **1.2 — Rewrite `P2_Results/README.md` against what the code actually produces.**
  - Column heading `Measured Result` → `Reported Result`
  - Remove the E4 and E5 **PASS** rows entirely (both cite quarantined files)
  - Mark E3's `Re-plan time: 2.58 s` as **modelled** (see 2.6)
  - Replace *"empirical campus trace playback"* — the traces are synthetic and the repo says so
  - Fix the broken links: every file reference is `file:///C:/Users/aksha/Introduction%20to%20CLoud%20Computing/…`,
    which resolves for nobody. Use repo-relative paths.
  *Acceptance:* every claim in the table maps to a file a reader can regenerate. **~30 min.**

- [ ] **1.3 — Settle which PRD is authoritative, and fix §7 provenance.**

  There are currently **four PRD artifacts in the tree**:

  | Path | Tracked | Notes |
  |---|---|---|
  | `docs/PRD_v5.pdf` | yes | **Authoritative.** Text byte-identical to root `prd.pdf` |
  | `prd.pdf` (root) | **no** | Redundant copy; differs only in PDF annotations |
  | `docs/PRD_v6.md` | yes | Identical to the P2 copy (same md5) |
  | `P2_Results/PRD_v6.md` | yes | Duplicate of the above |

  `PRD_v6.md:55-56` declares E4 and E5 *"confirmed core MVP deliverables"*, citing the two
  fabricated CSVs as justification. `docs/PRD_v5.pdf` §8 lists both under **Stretch (beyond the
  MVP)** and names E4–E5 as the first things to cut on slip. **v5.0 already gives you the out —
  take it.** Delete the duplicate v6 copies, or demote them to `archive/` with a header saying
  they were superseded.

  Then correct two provenance claims in §7 so they match `evaluation/manifest.json`:
  - Traces: *"logged with a Raspberry Pi 5 carried along real IISc routes … at 10 Hz"* → they are
    20–26 line synthetic files whose own headers say `short synthetic example for local testing`,
    and the manifest declares `"synthetic": true`.
  - Workload: MIMIC-IV and BurstGPT are cited but **neither dataset is in the repo**.
    `run_e7.py` generates Poisson arrivals with ward-round bursts and says so in its docstring.
    Reword to *"MIMIC-shaped synthetic arrivals."*
  *Acceptance:* one PRD ships; no provenance sentence contradicts the manifest. **~20 min.**

- [ ] **1.4 — Gate the fake completion control.**
  `static/app.js:89` renders `finish-btn` on any leased task; `:114 simulateOutputReady()` posts a
  synthetic `OUTPUT_READY` with `result_ref: local://outputs/${id}/delivery-confirmed`. No robot
  moves. SC-1 — the sole FR-21 acceptance criterion — is one click from "complete".
  ```js
  if (task.status === "leased" && window.NIMBUS_DEMO_CONTROLS) { … }
  ```
  Default off; set it from a server-rendered flag driven by `NIMBUS_DEMO_CONTROLS=1`.
  *Acceptance:* a default-config requester app cannot produce `OUTPUT_READY`.
  *Violates:* FR-21 credibility. **~10 min.**

- [ ] **1.5 — Repair or retire `netem.sh`.**
  It is cited as E4 evidence and has never run successfully. `PROFILE` and `IFACE` are assigned at
  `:17-18` then never referenced, so it executes `case "" in` and `tc qdisc add dev "" …`.
  Every `""` should be `"$PROFILE"` / `"$IFACE"`.
  If E4 stays retracted (1.3), delete the script instead of fixing it — an unused broken script
  cited as evidence is worse than no script.
  *Acceptance:* `./netem.sh mild_wan eth0` applies a qdisc, or the file is gone. **~10 min.**

---

## Phase 2 — Reproducibility

- [ ] **2.1 — Make `pytest` run on a clean checkout.**
  A bare `pytest` aborts before collecting anything: the installed `pytest_filter_subpackage`
  plugin uses the pre-8 `pytest_ignore_collect(path, config)` signature.
  Add to `pyproject.toml`:
  ```toml
  [tool.pytest.ini_options]
  addopts = "-p no:filter_subpackage"
  ```
  *Acceptance:* bare `python -m pytest -q` collects and runs. **~5 min.**

- [ ] **2.2 — Fix the one failing test.**
  `tests/test_packs.py::test_core_contains_no_domain_vocabulary` fails on a *comment* containing
  the word "wing" at `cloud/leasebuilder.py:53` (`"…an equal-cost corridor in the other wing."`).
  Reword to "…in the adjacent cluster's area."
  This turns "the suite doesn't run" into a quotable **518 passing**.
  *Acceptance:* `pytest -q` → 518 passed, 0 failed. **~5 min.**

- [ ] **2.3 — Make E5 reproducible, or re-title it.**
  `results/e5_edge.csv` reports takeover **2.40 s**, reconcile **12.30 s**, watchdog **0.05 s**,
  recovery **68.00 s** — all typed by hand. The *behaviours* are genuinely covered and passing:
  `edge/tests/test_outbox_resilience.py`, `test_failover_takeover.py`, `test_boundary_and_parking.py`.

  **Preferred:** a thin `scripts/run_e5.py` that invokes those tests and emits the CSV from their
  real timings.
  **Fallback if time is short:** re-title to `e5_verification_matrix.csv`, drop the invented
  seconds, keep the test names as evidence — which the file's own footer already does.
  *Acceptance:* every number in the file is produced by a command in the repo.
  *Violates:* NFR-6. **~1h preferred / ~15 min fallback.**

- [ ] **2.4 — Record the resolved SLM engine mode in every result row.**
  `EdgeSLMEngine(mode="auto")` probes Ollama with a 1 s timeout and silently becomes `"mock"`
  (`engine.py:48-55`). The mock is a keyword cascade (`engine.py:324`) that labels its output
  `decided_by="SLM"` with a synthetic `+15.0 ms` latency (`engine.py:238`).

  Ollama is **not running** in `peaceful_leakey`, so any rerun today lands in mock. Both E1 and E2
  construct with `mode="auto"`, which is why E2's "SLM vs rules" columns are near-identical — it is
  one keyword matcher scored against another, with nothing in the output saying so.

  - Add a resolved-mode field to `evaluate_decision`'s return, or expose `engine.mode`
  - Write it into every E1/E2 row
  - Refuse to write an E1/E2 artifact from `mock` unless `--allow-mock` is passed

  The fallback itself is a legitimate FR-3 behaviour. It must simply never be labelled `SLM`.
  *Acceptance:* no result row claims `SLM` when the engine resolved to `mock`. **~30 min.**

- [ ] **2.5 — Re-run E1 and E2 against real Ollama and publish the real numbers.**
  The generated report `edge/benchmarks/e1_results.md` (engine mode `ollama`) records
  **p95 = 22.022 s → FAIL** against NFR-1's ≤ 10 s. The submitted `e1_models.csv` claims
  **p95 = 0.495 s → PASS**. Factor of 44, and the repo contains both.

  The same report shows the pinned model answering `LOCAL_WAIT` to SC-01
  *"Routine waypoint progression in clear corridor"* in **all three trials** — correct answer is
  `DISPATCH_STEP` — and flipping between `LOCAL_WAIT` and `ESCALATE_UNEXPECTED` on SC-04.
  FR-3's "≥95% use an allowed action" passes only because those answers are *in the vocabulary*.

  ```bash
  ollama serve &  ollama pull qwen2.5:1.5b
  python3 edge/benchmarks/e1_benchmark.py qwen2.5:1.5b
  python3 edge/benchmarks/e2_benchmark.py
  ```
  Publish whatever p95 comes out, with the cause named (CPU-only host; cold load on trial 3).
  An honest NFR-1 miss with a stated mitigation is defensible at viva. A 0.495 s claim the repo
  itself contradicts is not.
  *Acceptance:* `e1_models.csv` and `e1_results.md` agree. **~45 min.**

- [ ] **2.6 — Re-run E3 through Bedrock, via the cassette.**
  The committed `results/e3/results.json` carries `"model": "stand-in"` — it was produced by the
  offline `RuleBasedLLM`, because `cloud/orchestrator.py:127` defaults to `NIMBUS_LLM=fake`.
  (The *deployed* cloud is genuinely Bedrock — `/llm-status` returns `live`,
  `tokens_estimated: false`. It is only the local default that is the stand-in.)
  ```bash
  NIMBUS_LLM=bedrock NIMBUS_BEDROCK_MODEL=apac.amazon.nova-lite-v1:0 \
    NIMBUS_CASSETTE=data/e3.cassette.json NIMBUS_CASSETTE_MODE=record \
    python scripts/run_e3.py --seeds 1
  NIMBUS_LLM=bedrock NIMBUS_CASSETTE=data/e3.cassette.json \
    NIMBUS_CASSETTE_MODE=replay python scripts/run_e3.py --seeds 10
  ```
  Commit the cassette: replays are free and byte-identical, which is NFR-6 evidence in itself.
  Keep calling the latency fields **modelled** — `run_e3.py:11` already says so and the field names
  (`modelled_completion_s`, `modelled_latency_s`) are honest. Only the write-up overstated them.
  *Acceptance:* `results/e3/results.json` names a real model. **~30 min + AWS cost.**

---

## Phase 3 — Functional defects

- [ ] **3.1 — Stop reporting a kinematic limit as a map closure.**
  `simulation/hospital_world/ground_robot_sdk/navigation.py:427`:
  ```python
  self._fail_and_stop(command_id, "planned bearing is behind the robot; "
                      "refusing an unsafe forward U-turn", CommandStatus.BLOCKED)
  ```
  Refusing the U-turn is **correct** — the platform has no rear sensor. Returning `BLOCKED`, the
  same status a physical obstruction produces, is not: the edge cannot distinguish the two, so a
  nominal return-leg turn escalates as `UNEXPECTED_EVENT` and the cloud writes a phantom closure
  onto the shared map — which then propagates to other clusters via FR-11 selective sharing.

  - Add `CommandStatus.REORIENT_REQUIRED`, distinct from `BLOCKED`
  - Ensure that path never reaches `mapstate.report()` in `cloud/react.py`
  - Then either a staging/reorientation manoeuvre in the controller, **or** a planner constraint in
    `planning.py` that never emits a rear-facing waypoint

  *Acceptance:* a return-leg turn completes without an escalation, and no phantom zone appears on
  `/map`. *Violates:* FR-17, and blocks FR-21. **~1–2 h.**

- [x] **3.2 — ~~Add a terminal state for a failed task.~~ WITHDRAWN — the original finding was wrong.**

  **Correction (2026-11-06).** Keeping the lease live and alerting the operator when no re-plan is
  admissible is *designed* behaviour, not a missing transition. Three existing tests pin it:

  | Test | Asserts |
  |---|---|
  | `test_ms2_handover.py::test_both_gates_closed_gives_a_reason_and_alerts_the_operator_instead_of_looping` | `repo.current(task)["version"] == 1` — lease stays live |
  | `test_ms2_react.py::test_no_route_left_alerts_the_operator` | `outcome.kind == "replan_failed"`, operator notified |
  | `test_cloud_api.py::test_replan_with_no_route_is_409_and_keeps_v1` | operator `POST /replan` → 409, lease preserved |

  The intended flow is: cloud cannot re-plan → **alert the operator** (PRD Table 2, UNEXPECTED
  EVENT → "alert the user") → operator pauses or cancels on the console (FR-22) → `orch.cancel()`
  releases the robot, the zones and the reserved item. Auto-terminating removes that decision from
  the human, which is the wrong default for a hospital.

  `replan_failed` also serves two different callers, which the withdrawn fix conflated: an
  operator's `POST /tasks/{id}/replan` is a *question* ("can you route around this?") and a "no"
  must keep the robot working, while an escalation where replan *and* reassign both failed is the
  plan dying under the robot. Any future change here must preserve both.

  An attempt at this was written, committed in `3810807` before verification, backed out, and
  replaced with `tests/test_failed_replan_lifecycle.py`, which pins the intended contract from the
  `apply_outcome` side — the gap that let the bad change pass its own tests.

- [ ] **3.2b — Decide whether operator-pending failures need a backstop.** *(the real, narrower gap)*
  Nothing bounds how long a task sits waiting for an operator. If nobody watches the console, the
  robot and the reserved item are held indefinitely: there is no timeout, and `replan_failed` is
  not in `TERMINAL_STATUSES` — correctly so, per 3.2 above.

  This is a **product decision, not a code inference**: auto-cancel after N minutes? An operator
  SLA with a louder alert? Leave it unbounded because a human is always on console during a
  demo? Whichever you choose, say so in the PRD rather than leaving it implicit.
  *Owner:* team. **~15 min to decide, ~45 min to implement if a timeout is wanted.**

## Phase 4 — Earn the acceptance evidence

> One genuine end-to-end trace is worth more than all four retracted CSVs.

- [ ] **4.1 — Run SC-1 end to end in real Gazebo and capture the decision log.**
  Gazebo **works** in these containers — both have `ign`, and `rclpy` imports once
  `/opt/ros/humble/setup.bash` is sourced, which `run_all.sh` does at line 22. Verified `HAVE_GZ=1`.
  A full stack (Gazebo + east-wing supervisor, `--sim-mode gazebo --gazebo-validation odometry`)
  has already been running for over an hour.

  Depends on **3.1** — without it the return leg escalates.
  Run with `KEEP_DATA=1` so the launcher does not delete the evidence it just produced.
  *Acceptance:* `decisions.jsonl` shows request → clarify → lease → sub-tasks → `OUTPUT_READY` →
  summary, with no synthetic escalation and no operator intervention.
  *Validates:* FR-21, SC-1, and credibly FR-4/6/9/13/16/20. **~1 h.**

- [ ] **4.2 — Repeat for SC-2 and SC-3.** PRD Milestone 2 is *"SC-1 to SC-3 run end to end through
  the app (MVP complete)."* SC-2 needs a corridor block then a gate closure (click a node in the
  viewer); SC-3 needs the isolation-ward statement path.
  *Acceptance:* three captured decision logs. **~1 h.**

- [ ] **4.3 — Real E4/E5 runs.** *Only if Phases 1–3 are done.* Stretch scope under PRD v5.0 §8 —
  leaving them retracted and unclaimed costs nothing against the MVP.

---

## Phase 5 — Hygiene

- [ ] **5.1 — Make `run_all.sh` stop eating the evidence.** Invert the default to `KEEP_DATA=1`,
  or move `data/` to `data.bak.<timestamp>/` instead of `rm -rf`. `run_all.sh:55`. **~10 min.**
- [ ] **5.2 — Clean `.gitignore`.** `data/` appears twice. The `prd.pdf` rule was commented out in
  an uncommitted edit — see 5.3 before deciding whether to restore it. **~5 min.**
- [ ] **5.3 — Delete the redundant root `prd.pdf`.** The PRD *is* already tracked, as
  `docs/PRD_v5.pdf`, and the README points there. Root `prd.pdf` is an untracked duplicate with
  identical text (verified: `pdftotext` diff → 0 lines) differing only in PDF annotations.
  Delete it and restore the `prd.pdf` ignore rule, rather than committing a second copy. **~2 min.**

---

## MVP traceability after this work

PRD v5.0 §8 MVP = C1 on T1–T5 end to end in Gazebo, plus FR-5, FR-8, FR-9, FR-18, FR-21, and E1–E3, E6.

| Req | Today | After | Gated by |
|---|---|---|---|
| FR-5 | Met *(working tree only)* | Met | 0.2 — the fix is unpushed |
| FR-8 | Met | Met | — |
| FR-9 | Met | Met | — |
| FR-18 | Met | Met | — |
| FR-20 | Met | Met | 0.1 — don't delete the log |
| FR-21 | **Blocked** | Met | 1.4, 3.1, 4.1 |
| FR-16 | Partial | Met | 3.2 |
| FR-17 | Partial | Met | 3.1 |
| FR-3 | Partial | Met | 2.4, 2.5 |
| NFR-1 | **Failing** | Honestly reported | 2.5 |
| NFR-5 | Partial | Met | 3.3 |
| NFR-6 | **Failing** | Met | 1.1, 2.1, 2.3, 2.6 |
| E1–E3, E6 | Partial | Met | 2.3, 2.5, 2.6 |

---

## What not to touch

Verified working. Changes here cost time and risk regressions:

- **Lease protocol** — versioning, message-ID dedup, conditional writes, circuit breaker, cassette replay
- **Domain separation** — `cloud/*.py` carries no hospital vocabulary; enforced by a guard test.
  The strongest architectural claim in the submission, and it is real.
- **Edge resilience behaviours** — outbox store-and-forward, node takeover, reconnection
  reconciliation, expiry parking. All pass as tests; only the write-up was fabricated.
- **`evaluation/manifest.json` + `audit_data.py`** — an honest, working NFR-5 check, and the thing
  that correctly flags the traces as synthetic.
- **`run_e3.py` / `run_e7.py`** — real harnesses, seeded, with modelled fields named as modelled.
  The template the other experiments should have followed.
