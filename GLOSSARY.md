# Nimbus — Cloud Orchestration Context

Person 4's slice of Team Nimbus (DS252): the shared wire contracts, the requester app,
the decision log, a thin local API that stands in for the Bedrock orchestrator, the
Terraform skeleton, and the cost counters. Terms come from Nimbus PRD v5.0 (26 Sep 2026).

## Authority and control

**Lease**:
Time-limited authority the cloud grants to exactly one edge cluster to run a task, holding
the task list, robots, allowed/forbidden zones, speed limit, deadline, expiry and version.
_Avoid_: permit, job ticket, contract

**Lease version**:
Monotonic number on every lease; only the highest version is ever honoured, and a new
version from the cloud replaces the old one outright.
_Avoid_: epoch, seq, revision

**Expiry**:
The lease timestamp after which the cluster finishes the current step and parks its robots
in safe spots; no new work may start past it.
_Avoid_: TTL, timeout

**Deadline**:
The time by which the task should complete (SC-1 uses 12 minutes); distinct from expiry,
which bounds the lease's authority.
_Avoid_: due date

**When-to-escalate**:
Part of the lease naming the conditions under which the edge must ask the cloud instead of
deciding alone.
_Avoid_: escalation policy, triggers

**Rule checker**:
The verifier that checks a proposed assignment (robots, zones, hospital rules, route)
before any lease is issued; the cloud never issues a lease the rule checker has not approved.
_Avoid_: validator, guard

## Messages

**Escalation**:
A short structured message the edge sends the cloud, always one of three types, always
carrying a message_id and lease_version.
_Avoid_: alert, notification

**CLARIFY**:
Escalation sent when the instruction or situation is too ambiguous or complex for the edge
model; carries the question and its possible readings.
_Avoid_: question request

**UNEXPECTED_EVENT**:
Escalation sent when the plan no longer works and nothing inside the lease fixes it (door
closed, robot failed, deadline at risk); carries what happened, where, and what robots saw.
_Avoid_: incident, failure report

**OUTPUT_READY**:
Escalation sent when a task produced a result (delivery photo, equipment list, log); carries
where the result is stored.
_Avoid_: result upload, completion message

**message_id**:
Unique identifier every message carries; a repeated, re-ordered or outdated message is
dropped rather than acted on twice.
_Avoid_: request id, correlation id

**Check-in**:
Short regular edge-to-cloud message that renews the lease; no LLM is involved in renewal.
_Avoid_: heartbeat, ping

## Work

**Plain-language request**:
Free-text ask from ward staff ("a paediatric pump to 2B, bed 12, soon") that the cloud turns
into a structured task, asking a question when unsure.
_Avoid_: order, ticket, job

**Task**:
The structured unit produced from a request: item, from, to, priority, deadline.
_Avoid_: request (after parsing), work order

**Robot sub-task**:
One command from the fixed set {go_to, wait, stop, photo, beep, report} that the edge
issues to a single robot.
_Avoid_: action, command, step

**Priority**:
The urgency class of a task; urgent work goes first, timed tasks supported.
_Avoid_: severity, weight

**Zone**:
A named area of the hospital building; a lease scopes its robots to allowed zones and away
from forbidden zones.
_Avoid_: region, area, ward (a ward is a zone instance, not the concept)

**Cluster**:
One edge cluster per wing: an edge model plus supervisor that always runs the robots under
it and holds exactly one lease at a time per robot.
_Avoid_: edge node (a node is one machine inside a cluster)

## Record keeping

**Decision log**:
Append-only record of every decision: who decided (cloud, edge or rule), inputs, output,
lease_version, timestamp, task_id.
_Avoid_: audit trail, event log

**Timeline**:
The replayable, ordered view of one task's decision log, including the requester's own
request and answer events.
_Avoid_: history, trace

**Clarifying question**:
The question the cloud asks the requester when a request is vague or conflicting; the
requester's answer is recorded like any other decision input.
_Avoid_: prompt, follow-up

## Deployment and team roles

**Cloud stand-in**:
Person 4's local fake orchestrator that emits leases and records decisions so the requester
app runs with no AWS account; Person 3 replaces it with the real Bedrock orchestrator.
_Avoid_: mock cloud, test double

**Reduced-autonomy mode**:
The state during a Bedrock outage in which leases are issued only by the deterministic
rule-based path and users are told autonomy is reduced (Person 3 owns this).
_Avoid_: fallback mode, offline mode
