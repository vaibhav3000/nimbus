# Cloud-edge messaging topics

The three topic patterns of the cloud-edge boundary (PRD v5.0 Fig 4 components
6–7). `cluster_id` is the edge cluster the topic concerns (SC-1: `east-wing`).
Every message carries a `message_id` and the lease version it belongs to
(FR-8); a repeated message is dropped, an outdated lease version is never
honoured. In the real AWS deployment leases go out and escalations/check-ins
come back through AWS IoT Core + SQS; in this slice **the outbox file
`data/outbox.jsonl` is the stand-in**: Person 2 reads it until a real IoT
client exists, and nothing is published anywhere else.

## `nimbus/{cluster_id}/lease`: cloud → edge

Carries the lease document (`contracts/lease.schema.json`). One publication
per lease issue; a new version replaces the old one outright.

```json
{
  "topic": "nimbus/east-wing/lease",
  "payload": {
    "lease_id": "lease-task-000002-v1",
    "version": 1,
    "priority": "urgent",
    "deadline_ts": "2026-10-01T20:11:22Z",
    "expiry_ts": "2026-10-01T20:14:22Z",
    "allowed_zones": ["2B-12"],
    "forbidden_zones": [],
    "speed_limit": 0.8,
    "assigned_robots": ["R1"],
    "task_sequence": [
      {"cmd": "go_to", "robot": "R1", "params": {"to": "2B-12"}},
      {"cmd": "beep", "robot": "R1", "params": {}}
    ],
    "cluster_id": "east-wing",
    "when_to_escalate": ["door_closed", "deadline_at_risk", "robot_failed"]
  }
}
```

## `nimbus/{cluster_id}/escalation`: edge → cloud

Carries the escalation document (`contracts/escalation.schema.json`), POSTed
to the existing `POST /escalations` route; see that schema for the three types
and their Table 2 payloads.

```json
{
  "topic": "nimbus/east-wing/escalation",
  "payload": {
    "type": "OUTPUT_READY",
    "message_id": "msg-0009",
    "lease_version": 1,
    "task_id": "task-000002",
    "payload": {"result_ref": "s3://nimbus-outputs/task-000002/photo-1.jpg"}
  }
}
```

## `nimbus/{cluster_id}/checkin`: edge → cloud

The regular renewal signal (no LLM involved, PRD Sec. 6). Accepted at
`POST /checkins`; recorded as one `edge` decision-log row and nothing more:
no re-planning. `message_id` dedupes exactly like escalations. Routing and
lease renewal are Person 3's; the stand-in does not validate the cluster.

```json
{
  "topic": "nimbus/east-wing/checkin",
  "payload": {
    "cluster_id": "east-wing",
    "lease_id": "lease-task-000002-v1",
    "lease_version": 1,
    "timestamp": "2026-10-02T10:00:00Z",
    "message_id": "msg-0010"
  }
}
```

---

## Addendum (Person 3, PROPOSED: additive, in use, awaiting P4 / P2 agreement)

Nothing above changes. These only add, so existing consumers keep working.

**`nimbus/{cluster_id}/renewal`: cloud → edge.** Sent when a check-in extends a live lease.
The lease **version does not change**; only `expiry_ts` moves. The lease schema is untouched.

```json
{"topic": "nimbus/east-wing/renewal",
 "payload": {"lease_id": "lease-task-000002-v1", "version": 1,
             "expiry_ts": "2026-10-18T09:06:00Z", "message_id": "renew-lease-task-000002-v1-1792314360"}}
```

**`nimbus/{cluster_id}/probe`: cloud → edge.** Sent only to a cluster that was alive and went
quiet (stale), before delegating. The edge answers with a heartbeat (below).

```json
{"topic": "nimbus/east-wing/probe",
 "payload": {"type": "PROBE", "cluster_id": "east-wing", "message_id": "probe-east-wing-1792314300", "ts": "2026-10-18T09:05:00Z"}}
```

**Optional fields on a check-in** (also accepted by `POST /fleet/heartbeat`, which needs no lease):

```json
{"robots": [{"id": "R1", "state": "idle|busy|failed|charging", "zone": "E-STORE", "battery": 0.9}],
 "stock":  {"wheelchair": 4, "infusion_pump_paediatric": 2}}
```

**Optional field on an UNEXPECTED_EVENT payload:** `segments: [{"segment_id": "E3", "state": "blocked|closed|crowded"}]`.
The cloud re-plans around these; without it, any known zone id named in `where` is used.

**Stale versions:** a check-in or escalation carrying a `lease_version` below the current one gets
HTTP 409 `{"error": "outdated lease_version", "current": N}`, so the edge learns the current version.
