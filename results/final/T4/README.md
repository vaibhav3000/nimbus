# T4: Blocked corridor; detour inside the lease zone

- Trials: 3  |  generated 2026-11-07T15:19:45Z  |  commit 81d3b18
- Pack: `packs/hospital_sim6_t2`  |  obstacle: `east_mid_hall`

## Scenario

- Mission: `Need a pediatric infusion pump at 2B-12, urgent.` through the real request -> allocation -> checker -> lease -> edge path.
- A persistent corridor obstacle blocks the route; a valid detour exists INSIDE the lease's zones.
- The edge<->cloud link is a BAD link (degraded latency/jitter/loss) for the whole mission.

## Expected contrast

- **C1**: edge SLM decides the detour locally -> the bad link is not on the decision path -> **fastest**.
- **C2**: cloud-only relay pays one cloud round trip per blocked step over the bad link -> **stalls on the link** (slowest; escalates if the round trip can't resolve).
- **C3**: local finite-state rules detour -> also fast (another classical-baseline point).

## Why T4 uses a local cloud, not the deployed AWS endpoint

T1 and T8 run C1/C2 against the deployed clouds. **T4 does not**, for two test-scoped reasons:

1. **The detour scenario needs custom map/scenario changes not in the real hospital map.** T4 requires a blocked corridor that still has a valid detour *inside the lease zone*; staging that reliably uses a test-scoped configuration (a compressed-timing pack and a deterministically placed obstacle) that is not part of the actual deployed hospital map. We shape the scenario locally rather than reshape the real hospital map.
2. **The bad link can't be injected against a managed endpoint.** The degradation is an in-process FaultProxy (latency/jitter/loss), which cannot be applied to a managed HTTPS endpoint from this host (no tc/NET_ADMIN).

T4 uses a LOCAL cloud, not the deployed endpoint, for two test-scoped reasons: (1) the 'blocked corridor with a detour INSIDE the lease zone' case needs a controlled map/scenario configuration (a compressed-timing pack and a deterministically placed obstacle) that is NOT part of the actual deployed hospital map -- the detour scenario requires custom map/scenario changes, so we run it locally rather than reshape the real hospital map; and (2) the bad link is injected with an in-process FaultProxy, which cannot be applied against a managed HTTPS endpoint from this host. T1/T8 need neither and use the deployed C1/C2 endpoints.

Edge = the real supervisor (C1 SLM / C2 relay / C3 rules), kinematic mock robot, driven in-process. A persistent corridor obstacle forces a tactical decision; a detour exists inside the lease. The edge<->cloud link is degraded for the whole mission by an in-process FaultProxy (latency+jitter+loss), not tc. C1 and C3 decide the detour LOCALLY (link not on the decision path) and are fast; C2 must round-trip to the cloud per decision and stalls on the bad link. Completion time and decision latency are measured, not inferred.

## Plots

![completion time](plots/completion_time.png)
![decision latency](plots/decision_latency.png)

## Results (per trial)

| mode | profile | outcome | time-to-outcome (s) | obstacle decision latency (ms) | decided by | deadline |
|---|---|---|---|---|---|---|
| C1 | severe | delivered | 8.398 | 15.1 | SLM | met |
| C2 | severe | STALLED -> escalated (relay could not resolve over the bad link) | 5.22 | 1711.1 | C2_RELAY | FAIL |
| C3 | severe | delivered | 12.039 | 1.5 | C3_FSM | met |

Raw per-trial records are in `raw/trials.jsonl`; `summary.csv` is computed from them.
Per-decision edge telemetry (with latency) is in `raw/decision_log.jsonl`.
