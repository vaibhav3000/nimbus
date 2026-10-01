# ASSUMPTIONS.md — Person 4's readings of PRD v5.0 gaps

The mediator instructed: pick the simplest reading, record it, continue. Every
entry below is a PRD detail the scope left open and the reading chosen.

1. **Old Nimbus workspace is v3.1, not v5.0.** `Projects/NIMBUS_WORKSPACE`
   targets PRD v3.1 (corridor/waypoint/epoch/seq leases, L0–L3 rung ladder).
   Its schema documents do not match the v5.0 field list in the scope, so they
   were not reused as documents — see REUSE.md for what was carried over
   (style, dependency set, validation pattern).

2. **Project location.** New directory `Projects/nimbus-p4/`, next to the old
   workspace; the mediator's "current workspace" spans the whole
   `Downloads/Cloud Computing` folder (PRD and old implementation both live in
   `Projects/`). Git was initialised here because none existed; the code-review
   step needs a diff, and the team can push this to the course GitHub later.

3. **Lease timestamps.** `deadline_ts` = issue time + 12 min for urgent tasks
   (SC-1's 12-minute deadline; routine tasks get 60 min). `expiry_ts` =
   deadline + 3 min, so the authority window outlives the deadline it guards.
   Both ISO 8601 UTC. Cross-field rule `deadline_ts <= expiry_ts` enforced in
   `contracts.py`, not JSON Schema (same pattern the old workspace used).

4. **Priority vocabulary.** PRD says "priority levels and deadlines; urgent
   work goes first" without naming levels. Simplest: `["routine", "urgent"]`.

5. **`when_to_escalate` shape.** PRD: the lease holds "when to ask the cloud".
   Simplest structured reading: an array of named condition strings
   (`door_closed`, `deadline_at_risk`, `robot_failed`). Edge semantics are
   Person 2's.

6. **Decision-log `who` includes `requester`.** FR-20 fixes decisions as
   cloud/edge/rule, but acceptance requires the timeline to show requester
   rows. Reading: one homogeneous log schema whose `who` enum is
   `[requester, cloud, edge, rule]`; *decisions* are only ever cloud/edge/rule
   rows; requester rows carry the human's words and answers. This keeps
   `GET /timeline` one format for replay.

7. **Clarify heuristic (FR-13 stand-in shape).** The stand-in cannot be an
   LLM. Rule: a request naming bed 2B-12 (a paediatric bed, per SC-1) without
   naming a pump type produces the question "2B-12 is a paediatric bed. Do you
   need a paediatric pump?" with readings `[paediatric infusion pump,
   standard infusion pump]`; any answer resolves it and leases. Everything
   else leases immediately. The question reuses the CLARIFY payload shape from
   PRD Table 2 so the app speaks the wire format.

8. **Lease v1 content.** SC-1: East-wing cluster, urgent, allowed zone derived
   from the request text (2B-12 → zone `2B-12`), one robot (R1), a two-step
   task sequence (`go_to` + `beep`), speed limit 0.8 m/s,
   `when_to_escalate` as above. Zone/robot assignment is deliberately crude —
   the real assignment is Person 3's rule checker + scheduler.

9. **`/escalations` ingress exists though Person 3 owns escalation handling.**
   Needed so (a) the app can show a result (OUTPUT_READY completes the task),
   (b) FR-8's dedup/staleness shape is observable at a public boundary, and
   (c) Person 3 has a drop-in ingress: the route, guards and log rows stay,
   only the reaction logic gets replaced. UNEXPECTED_EVENT/CLARIFY reactions
   (re-plan, map update) are NOT implemented — rows are recorded only.

10. **Cost counters record stand-in values.** Input tokens = `len(text)//4`
    (crude stand-in tokenizer), output tokens = 150 per cloud decision (the
    PRD's edge decision cap, used here as a plausible per-decision constant),
    Fargate GB-s = flat 0.001 per decision, IoT = 1 per lease published,
    DynamoDB = rows written, SQS = escalations, S3 = OUTPUT_READY results.
    Prices are never applied — PRD Sec. 7 keeps p_* symbolic. No Cost
    Explorer calls (scope).

11. **API Gateway integration is a MOCK placeholder.** A real API Gateway →
    ECS Fargate hop needs a VPC link + NLB (or internal ALB) — infra sizing
    beyond a skeleton. Comment in `main.tf` marks the replacement point.

12. **Terraform binary.** None was installed on this machine, so a portable
    Terraform 1.9.8 was downloaded into `tools/` (gitignored) to make
    `terraform validate` pass honestly. Provider lockfile is checked in.

13. **Timeline row order is the replay order.** Rows are appended in causal
    order (requester → cloud → rule → cloud; edge rows when escalations
    arrive); `GET /timeline` returns append order; no separate sequence
    number field.

14. **FR-8 dedup marks a message id only on acceptance.** A delivery that
    fails validation or task lookup does not burn its `message_id`, so an
    honest retry still executes (a repeated id is ignored only after its
    message was accepted once). Simplest safe reading of "repeated … ones are
    ignored".

15. **The app's "Demo: edge sends OUTPUT_READY" button.** FR-21 requires the
    app to show a result, but no edge exists in this slice; the button posts a
    real OUTPUT_READY escalation so the result path is demoable. It is demo
    glue, clearly labelled — Person 2's edge replaces it.

16. **IAM policy carries two extras beyond the minimal enumeration.**
    `events:PutEvents` (the service must publish onto the EventBridge bus the
    skeleton declares) and `iot:Subscribe` (an always-on container consuming
    check-ins needs it, not just connect/publish). Both are scoped to the
    specific bus/topic ARNs, keeping NFR-5's least-privilege intent.

17. **Code-review findings (both axes) were fixed before handoff:** duplicate
    response in the escalation handler removed, task-id allocation moved under
    a lock with a monotonic counter, dedup moved to mark-on-acceptance,
    decision-row construction deduplicated through one helper, app HTML
    interpolation escaped, and the stale-version test expectation aligned with
    what is publicly reachable (see SPEC-P4.md).
