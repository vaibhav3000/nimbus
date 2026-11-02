# For P4: what P3 changed in your files, and what we need (MS2)

All of this is on branch `p3-ms2`. Your tests still pass except `tests/test_mqtt_transport.py` under `NIMBUS_BACKEND=aws`
(three tests; they fail the same way on `main`: they read a local `outbox.jsonl`).

## Your files, changed

| File | Change | Why |
|---|---|---|
| `server.py` | `apply_outcome()`: one function for HTTP and MQTT (the two copies had drifted) | one place to record a decision |
| `server.py` | Control: 409 for cancel/pause on a completed, cancelled or rejected task; cancel/pause publish a **stop lease** | FR-22: the stop must reach the robots |
| `server.py` | Check-in: validated before it renews; 409 if the cluster does not hold the lease | a wrong cluster could extend a lease |
| `server.py` | Escalations call `orch.react` (UNEXPECTED_EVENT, CLARIFY); OUTPUT_READY of a non-final leg leases the next leg; a requester's answer to an edge CLARIFY is relayed to the cluster | FR-9, FR-16 |
| `server.py` | New: `POST /relay/decide` (C2 only), `GET /notifications?audience=&task_id=`; `GET /fleet` has `places`; task view has `legs`, `leg_index`, `children`, `parent` (all additive) | C2, notices, hand-over, SC-3 |
| `server.py` | One message can create several tasks (`children`, each with `parent`); a statement ("Ward 2C is now isolation...") completes its own task with a summary | FR-13, SC-3 |
| `tests/test_api.py` | `test_checkin_unknown_cluster_still_records` became `test_checkin_from_wrong_cluster_is_refused` | see above |
| `cost.py` | New counter `llm_calls` | NFR-4 needs model calls per task |
| `infra/terraform/main.tf`, `variables.tf` | Fargate task gets `NIMBUS_BACKEND`, `NIMBUS_LLM`, `NIMBUS_BEDROCK_MODEL`, `NIMBUS_MODE`, `NIMBUS_DDB_PREFIX`; variables `bedrock_model_id`, `nimbus_mode` | without them a deployed service runs the offline stand-in |

## Contract addendum to merge (yours to approve)

Add to `contracts/topics.md` and the IoT rule / `transport.md`:

```
nimbus/{cluster_id}/notice   (cloud -> edge)   optional
{"type": "ANSWER|WARNING|MAP|STOP", "text": "...", "message_id": "notice-<hash>", "ts": "...",
 "task_id": "...", "lease_version": 2}
```

- Terraform: add `notice = "nimbus/+/notice"` next to `renewal`/`probe` in `main.tf`.
- `mqtt_transport.py` needs no change (`publish` takes any topic); `CANONICAL_MESSAGE_TYPES` could gain `"notice"`.
- Optional field on UNEXPECTED_EVENT payloads: `robot_zone` (a map node id).
- Stop lease (no schema change): a lease whose `task_sequence` is `[{"cmd": "stop"}]`. See `docs/p2-edge-contract-ms2.md`.

## Ask

- The operator console could show `GET /notifications?audience=operator` (isolations, re-assignments, failed re-plans).
- Persistent closures, restrictions and notices are in memory today; a restart forgets them. Tell us if you want them in DynamoDB.
