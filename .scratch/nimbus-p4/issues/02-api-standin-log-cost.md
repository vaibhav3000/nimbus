# 02: Local API, cloud stand-in, decision log, cost hooks (immediate-lease path)

**What to build:** A thin HTTP API (stdlib server) that accepts a plain-language request,
stores it, emits a fake lease v1 that validates against the lease schema, writes
decision-log rows (requester, cloud, rule), returns status, and records PRD Sec. 7 cost
counters. The cloud stand-in issues a lease immediately for requests that need no
clarification. State is file-backed so a restart keeps it. Curl-demonstrable with no
browser.

**Blocked by:** 01 (Contracts and validators).

**Status:** ready-for-agent

- [ ] `python server.py` boots API + static app hosting on one port; data survives restart
- [ ] POST /requests creates a task and returns task_id + status; a lease v1 validating against the lease schema is stored and visible via GET /tasks/{id}
- [ ] GET /tasks/{id}/timeline returns the full ordered decision-log rows for the task, including requester, cloud and rule rows
- [ ] Repeated message_id on POST /requests is ignored: the same task is returned with duplicate flagged, no new rows (FR-8 shape)
- [ ] GET /cost reports counters for every PRD Sec. 7 formula term and moves as requests are served
- [ ] Tests hit only the public HTTP surface and the published schemas
