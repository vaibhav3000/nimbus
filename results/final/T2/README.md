# T2: Urgent delivery under a sustained WAN outage

- Trials: 3  |  generated 2026-11-07T15:19:44Z  |  commit 81d3b18
- Pack: `packs/hospital_sim6_t2` (compressed timing)  |  obstacle: `east_mid_hall`
- tc available: False  |  ping available: False

## Scenario

- Mission: `Need a pediatric infusion pump at 2B-12, urgent.` through the real request -> allocation -> checker -> lease -> edge path.
- Cloud: local, launched per `NIMBUS_MODE` (C2 is a real C2 cloud; relay never hits a C1 endpoint).
- Edge: the real `edge/supervisor/supervisor.py` driven in-process (C1 edge SLM / C2 cloud relay / C3 local rules).
- Outage: cloud<->edge link cut (local cloud paused) the instant the lease is ingested, held to end of mission.
- A persistent corridor obstacle forces a tactical decision mid-route while the WAN is down.

## Why T2 uses a local cloud, not the deployed AWS endpoint

T1 and T8 point C1/C2 at the deployed clouds (and requests show in the operator GUI). **T2 cannot**, because T2 is fundamentally a **network-disruption simulation** -- its purpose is to cut and restore the cloud<->edge link on a controlled schedule, which cannot be done to a managed AWS endpoint from this host. Concretely its mechanism is a precisely-timed WAN cut:

1. The outage is created by **`SIGSTOP`-ing the cloud process** the instant the lease is ingested and `SIGCONT`-ing it at mission end. A managed service (API Gateway v2 + Fargate) cannot be paused, and its cloud<->edge link cannot be severed at a chosen millisecond and held. Owning the cloud process locally is the only way to cut it deterministically.
2. The C1-vs-C2 contrast needs a **short lease** so C2's offline stall reaches a terminal state in ~15 s rather than ~15 min. T2 uses a compressed-timing pack (`packs/hospital_sim6_t2`: 1 min urgent deadline + 8 s grace); the deployed cloud is fixed to the production pack, which must not be mutated.
3. T2 drives the **real edge supervisor in-process** to inject the obstacle and read the `EXPIRED_PARKED`/`ESCALATED` outcome directly; that supervisor must target a cloud whose process this script controls.
4. Running it against the deployed cloud would also push dozens of throwaway urgent tasks and a forced outage onto the shared endpoint that T1/T8 depend on.

The local cloud still runs the **real** orchestrator, allocator, rule checker, lease repo and `/relay/decide` in the requested `NIMBUS_MODE` -- only its host and timing differ from the deployment.

## Expected contrast

- **C1**: edge SLM detours locally, no cloud needed -> **delivered**.
- **C2**: cloud-only relay can't reach the cloud to decide -> a thin relay has no local brain, so it **halts and escalates to an operator**; with the WAN down the escalation is stuck in the offline outbox (and the lease is never renewed) -> urgent delivery **fails**.
- **C3**: local finite-state rules take a map-valid detour -> **delivered** (classical baseline copes with this particular event).

Edge = the real supervisor (C1 SLM / C2 relay / C3 rules) driven in-process. Outage = the cloud<->edge link cut by pausing the local cloud right after the lease is granted, held to end of mission. A persistent corridor obstacle forces a tactical decision. Compressed-timing pack (1 min urgent deadline + 8 s grace) so C2's lease expires within a short demo window; the mechanism is identical at production scale. RTT/loss/throughput/recovery are measured from HTTP probes and the traffic generator, never inferred from requested settings. Real tc/netem is applied only where tc exists.

## Plots

![outcome](plots/outcome.png)
![network rtt](plots/network_rtt.png)

## Results (per trial)

| mode | outcome | completion (s) | time-to-fail (s) | deadline | rtt (ms) | loss% | recovery (s) | llm |
|---|---|---|---|---|---|---|---|---|
| C1 | delivered | 12.648 | None | met | 1.53 | 60.0 | None | 1 |
| C2 | HALTED (no local brain; escalated to operator, but WAN down -> escalation stuck in offline outbox) - delivery FAILED | None | 13.937 | FAIL | 1.62 | 50.0 | 0.95 | 1 |
| C3 | delivered | 12.967 | None | met | 1.52 | 50.0 | 0.94 | 0 |

Raw per-trial records are in `raw/trials.jsonl`; `summary.csv` is computed from them.
