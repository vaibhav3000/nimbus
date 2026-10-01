# 03: Clarify flow, escalation ingress, SC-1 end to end

**What to build:** The cloud stand-in's clarifying behaviour for vague SC-1-style requests
(bed 2B without a pump type), the requester's answer endpoint that resolves the question
and issues lease v1, and a POST /escalations ingress that accepts schema-valid
escalations: dedup by message_id, outdated lease_version dropped, OUTPUT_READY completing
the task with its result_ref. The full SC-1 story ("Need an infusion pump at 2B-12,
urgent." → question → answer → lease) works over HTTP.

**Blocked by:** 02 (Local API, cloud stand-in, decision log, cost hooks).

**Status:** ready-for-agent

- [ ] POST /requests with the SC-1 text returns a status that includes the clarifying question; the question and its possible readings validate as a CLARIFY escalation payload
- [ ] POST /requests/{id}/answer records the answer and issues lease v1; the timeline gains the answer and lease rows
- [ ] POST /escalations validates against the escalation schema; repeated message_id is ignored; an escalation with a lease_version lower than the task's current lease is dropped as outdated
- [ ] An OUTPUT_READY escalation marks the task completed and its result_ref visible via GET /tasks/{id}
- [ ] Tests cover the whole SC-1 path plus dedup and staleness over public HTTP only
