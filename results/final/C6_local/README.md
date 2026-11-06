# C6_local: Re-plan and hand-over latency (FR-16)

- **Claim:** After an unexpected event the cloud issues a new plan (new lease version, hand-over, wait or operator alert) within about 30 s.
- **PRD pass bar:** New plan issued within about 30 s on a good network (FR-16).
- **Measured:** decision p95 0.02 s, p50 0.02 s over 32 events; 18 new lease versions, 18/18 avoid the closed zone or failed robot; outcomes {'rerouted lease': 10, 'operator alerted (no route left)': 14, 'another robot took over': 8}
- **Verdict:** PASS
- **Source of numbers:** mock_llm (local-C1, model rule-based-stand-in)
- **Command:** `python experiments/p3/c6_replan.py --target local`
- **Commit:** 81f0b93-dirty, run 2026-11-06T14:55:49Z to 2026-11-06T14:56:40Z

## Plots

![replan_latency_cdf](plots/replan_latency_cdf.png)
![decision_breakdown](plots/decision_breakdown.png)
![actions](plots/actions.png)

## Notes

- Decision time is the POST /escalations round trip from this machine, so it includes the network to the cloud.
- On this map a delivery from the south store to 2B or 2C has only one corridor inside its wing, so blocking it often leaves no route in the cluster: then the cloud waits, hands over, or alerts the operator. Those are plans too; the 'actions' plot shows which.
- Every event's zone is reopened and its task cancelled before the next event.

Raw per-trial records are in `raw/`; `summary.csv` is computed from them by the same script.
