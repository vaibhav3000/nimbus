# For P2 (edge): what the cloud now sends and expects (MS2)

No lease field is added or removed (`contracts/lease.schema.json` is unchanged). Everything below is behaviour on
top of messages you already handle. The cloud tests (`tests/test_ms2_*.py`) run `scripts/edge_sim.py` as the edge;
that script is a working example of each rule.

## 1. Stop lease (how the cloud ends or pauses a task)

A **stop lease** is an ordinary lease with a higher version for the same task:

```json
{"lease_id": "lease-task-000007-v3", "version": 3, "cluster_id": "east-wing",
 "assigned_robots": ["R1"], "allowed_zones": ["E4"],
 "task_sequence": [{"cmd": "stop", "params": {}}], "...": "other fields as in the lease it replaces"}
```

Recognise it by `task_sequence == [{"cmd": "stop"}]`. When you receive one:

1. Treat it as the new highest version for that task (drop the older version, as for any replacement, FR-8).
2. Finish the current motion step, then park the robot where it is (`allowed_zones[0]` is that zone).
3. Do not start any new sub-task, do not check in for it, and do not send OUTPUT_READY for it.

It is sent when the operator cancels or pauses, when a new restriction recalls work, and to the *old* cluster when a task
is handed to another cluster (it carries the same version as the new cluster's real lease; you only see your own topic).

## 2. Hand-over legs (one task, several leases)

A task that crosses clusters is split into legs. Each leg is a normal lease; the next leg is the next version of the
same task, sent to the other cluster. **Send OUTPUT_READY when your leg's last step is done** (as now). If the lease's final
`go_to` target is a hand-off zone (a gate or shared hall), that is the end of your leg, not of the task.

## 3. `notice` topic (cloud to edge, optional to read)

Topic `nimbus/{cluster_id}/notice`. Payload: `{type, text, message_id, ts, task_id?, lease_version?}`.

| type | meaning | what to do |
|---|---|---|
| `ANSWER` | answer to your CLARIFY | use `text` for the decision that was waiting |
| `WARNING` | something affects your work (a task was handed away) | log it; if it names a task you hold, a stop lease follows |
| `MAP` | a zone is blocked, closed or restricted | avoid that zone in local detours until it expires (about 15 min) |
| `STOP` | reserved | none |

Over HTTP these appear in `GET /outbox` with topic `nimbus/{cluster}/notice`. If you ignore the topic nothing breaks.

## 4. Things that make the cloud's plans correct

- **Robot position:** send `robots[].zone` in check-ins and heartbeats as a **map node id** (e.g. `east_waiting_approach`),
  not `home`/`transit`. A re-plan starts from there, and a hand-over needs it to know where to collect the item.
- **UNEXPECTED_EVENT:** send `segments: [{"segment_id": <node id>, "state": "blocked|closed|crowded"}]`.
  Optional `robot_zone` (a node id) in the payload says where the robot parked.
- A re-plan or hand-over arrives as a new lease version; a 409 on a check-in or escalation means "you hold an old version".
