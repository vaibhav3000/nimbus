# C6: Re-plan and hand-over latency (FR-16)

- **Claim:** After an unexpected event the cloud issues a new plan (new lease version, hand-over, wait or operator alert) within about 30 s.
- **PRD pass bar:** New plan issued within about 30 s on a good network (FR-16).
- **Measured:** decision p95 0.03 s, p50 0.02 s over 32 events; 18 new lease versions, 18/18 avoid the closed zone or failed robot; outcomes {'rerouted lease': 10, 'operator alerted (no route left)': 14, 'another robot took over': 8}
- **Verdict:** PASS
- **Source of numbers:** real_bedrock (local-C1-bedrock, model apac.amazon.nova-lite-v1:0)
- **Command:** `python experiments/cloud/c6_replan.py --target local-bedrock`
- **Commit:** 91a3488, run 2026-11-06T17:12:34Z to 2026-11-06T17:13:27Z

## Plots

![replan_latency_cdf](plots/replan_latency_cdf.png)
![decision_breakdown](plots/decision_breakdown.png)
![actions](plots/actions.png)

## Notes

- Decision time is the POST /escalations round trip from this machine, so it includes the network to the cloud.
- On this map a delivery from the south store to 2B or 2C has only one corridor inside its wing, so blocking it often leaves no route in the cluster: then the cloud waits, hands over, or alerts the operator. Those are plans too; the 'actions' plot shows which.
- Every event's zone is reopened and its task cancelled before the next event.
- With the real Nova Lite model enabled, all 32 reactions were still decided by the rules (`decided_by` is `rules`, `model_calls` is 0 in every record). The cloud asks the model only when the rules cannot choose, and that did not happen for these incidents. So the decision time is the same as with the stand-in model, and this run does not show the cost of a model call in a re-plan.

Raw per-trial records are in `raw/`; `summary.csv` is computed from them by the same script.
