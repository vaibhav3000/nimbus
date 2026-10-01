# 01: Contracts and validators

**What to build:** The four wire contracts (lease, escalation, robot sub-task, decision-log
entry) as JSON Schema files in `contracts/`, loadable and checkable from a small validation
module. A developer can validate a fixture lease and all three escalation payloads against
the schemas and see bad documents rejected. This is the team's wire format, published for
Person 2 and Person 3.

**Blocked by:** None (can start immediately).

**Status:** ready-for-agent

- [ ] `contracts/lease.schema.json` requires exactly the v5.0 field set (lease_id, version, priority, deadline_ts, expiry_ts, allowed_zones, forbidden_zones, speed_limit, assigned_robots, task_sequence, cluster_id, when_to_escalate); version >= 1; priority in {routine, urgent}; task_sequence items are robot sub-tasks
- [ ] `contracts/escalation.schema.json` enforces the three types with PRD Table 2 payload keys per type, plus message_id and lease_version on every escalation
- [ ] `contracts/robot_subtask.schema.json` allows only cmd in {go_to, wait, stop, photo, beep, report}
- [ ] `contracts/decision_log_entry.schema.json` requires who, inputs, output, lease_version, timestamp, task_id
- [ ] A validation module loads each schema, checks it, validates documents, and enforces the cross-field rule deadline_ts <= expiry_ts outside JSON Schema
- [ ] Tests prove a valid SC-1 lease and the three escalation payloads pass, and missing-field / bad-cmd / extra-property documents fail
