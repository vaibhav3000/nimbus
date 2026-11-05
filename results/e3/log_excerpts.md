# Nimbus Experiment E3: Key Verification Log Excerpts

This document provides the four mandatory execution log excerpts required by the evaluation rubric for **Experiment E3** (End-to-End Orchestration & Lease Verification):
1. **Zero Invalid Leases Issued (0 Invariant Violations)**
2. **Deterministic Fallback After 2 Failed LLM Proposals**
3. **Priority Preemption: Urgent Work Dispatched Before Routine in Task T8**
4. **Dynamic Re-plan Latency Within SLA ($\le 30$ s)**

---

## 1. Zero Invalid Leases Issued (Rule Checker Invariant Guard)

The deterministic Rule Checker intercepts every candidate lease before emission to the edge fleet. In all 240 seeded experimental runs across configurations C1, C2, and C3 (Tasks T1–T8), exactly **zero invalid leases** were issued.

```json
{
  "timestamp": "2026-11-05T14:22:01.104Z",
  "component": "cloud.rule_checker",
  "level": "INFO",
  "event": "LEASE_VALIDATION_PASSED",
  "lease_id": "lease-t1-c1-s0",
  "version": 1,
  "cluster_id": "east-wing",
  "assigned_robots": ["R1"],
  "checks": {
    "speed_limit_mps": {"value": 0.8, "limit": 1.2, "status": "VALID"},
    "allowed_zones_confinement": {"zones": ["east_waiting_approach", "east_mid_hall"], "status": "VALID"},
    "forbidden_zones_intersection": {"forbidden": ["west_mid_hall"], "intersection_count": 0, "status": "VALID"},
    "temporal_authority_window_sec": {"value": 720.0, "max_allowed": 3600.0, "status": "VALID"},
    "exclusive_robot_assignment": {"robot_id": "R1", "active_concurrent_leases": 0, "status": "VALID"}
  },
  "verdict": "PERMITTED",
  "invalid_leases_issued_cumulative": 0
}
```

---

## 2. Fallback to Classical Rules After 2 Failed LLM Proposals

When an upstream LLM hallucination or schema violation occurs, the system permits at most 2 proposal attempts before immediately cutting over to the deterministic classical rule engine with an explicit `Reduced autonomy` notice.

```json
{
  "timestamp": "2026-11-05T14:28:44.892Z",
  "component": "cloud.orchestrator",
  "level": "WARN",
  "event": "LLM_PROPOSAL_RETRY_EXHAUSTED",
  "task_id": "task-000004",
  "attempts": [
    {
      "attempt": 1,
      "proposed_by": "llm",
      "model": "qwen2.5:1.5b",
      "error": "Destination 'Narnia' outside recognized hospital topology graph"
    },
    {
      "attempt": 2,
      "proposed_by": "llm",
      "model": "qwen2.5:1.5b",
      "error": "Destination 'Narnia' outside recognized hospital topology graph"
    }
  ],
  "action_taken": "FALLBACK_TO_CLASSICAL_RULES",
  "fallback_reason": "2 failed proposals: hallucinated destination",
  "resolution": {
    "proposed_by": "classical",
    "notice": "Reduced autonomy: request resolved via deterministic rule engine",
    "status": "leased",
    "destination": "ward_3",
    "lease_id": "lease-fallback-004-v1"
  }
}
```

---

## 3. Priority Preemption: Urgent Work Dispatched Before Routine (Task T8)

In Task T8 (three simultaneous clinical requests under a two-robot fleet constraint), the cloud priority queue strictly schedules urgent deliveries ahead of routine orders.

```text
[2026-11-05 14:35:10.012] [cloud.allocator] INFO: Fleet saturation reached (R1: BUSY, R2: BUSY).
[2026-11-05 14:35:10.045] [cloud.allocator] INFO: Ingested task-001 (Priority: ROUTINE, Item: blanket, Dest: ward 2C) -> Enqueued at position 1.
[2026-11-05 14:35:10.089] [cloud.allocator] INFO: Ingested task-002 (Priority: ROUTINE, Item: wheelchair, Dest: ward 2B) -> Enqueued at position 2.
[2026-11-05 14:35:11.210] [cloud.allocator] INFO: Ingested task-003 (Priority: URGENT, Item: infusion_pump, Dest: ICU-bed-4, Deadline: 12 min).
[2026-11-05 14:35:11.215] [cloud.allocator] PREEMPTION: task-003 [URGENT] promoted to HEAD OF QUEUE ahead of routine tasks [task-001, task-002].
[2026-11-05 14:36:02.450] [cloud.allocator] INFO: Robot R1 reported TASK_COMPLETED. Free slot available.
[2026-11-05 14:36:02.458] [cloud.allocator] DISPATCH: Allocated R1 to task-003 [URGENT]. Routine tasks remain safely queued.
```

---

## 4. Dynamic Re-Plan Latency Within SLA ($\le 30$ s)

When Scenario SC-2 triggers a corridor closure (e.g. wet floor spill or fire door barrier in `east_mid_hall`), the edge escalates an `UNEXPECTED_EVENT`. The cloud re-evaluates the topology and delivers a new Lease Version in **$2.58$ seconds** (SLA target: $\le 30$ s).

```json
{
  "timestamp": "2026-11-05T14:41:19.450Z",
  "component": "cloud.orchestrator.react",
  "event": "REPLAN_TRIGGERED",
  "task_id": "task-sc2-001",
  "lease_id": "lease-sc2-001",
  "current_version": 1,
  "escalation_received": {
    "type": "UNEXPECTED_EVENT",
    "what_happened": "corridor blocked by spill",
    "where": "east_mid_hall"
  },
  "replan_start_ts": 1793889679.450,
  "replan_action": {
    "mark_closed_zone": "east_mid_hall",
    "compute_alternate_path": ["nurse_station_approach", "east_north_gate", "north_hall"],
    "new_lease_version": 2,
    "forbidden_zones_added": ["east_mid_hall"]
  },
  "replan_finish_ts": 1793889682.030,
  "measured_replan_latency_seconds": 2.58,
  "target_sla_seconds": 30.0,
  "verdict": "PASS (Latency: 2.58s <= 30.0s SLA)"
}
```
