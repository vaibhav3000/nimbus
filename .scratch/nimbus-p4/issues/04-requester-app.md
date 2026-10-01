# 04: Requester app (FR-21) static page

**What to build:** A dependency-free static page served by the same server: a text box for
plain-language requests, display of the clarifying question with an answer box, live task
status and result, and the task timeline. SC-1 ("Need an infusion pump at 2B-12, urgent.")
is clickable end to end, and a browser refresh re-renders everything from the API with no
lost state.

**Blocked by:** 03 (Clarify flow, escalation ingress, SC-1 end to end).

**Status:** ready-for-agent

- [ ] The page loads at the server root and needs no build step or new dependency
- [ ] Sending the SC-1 request shows the clarifying question; answering it shows the lease and status; the result appears once an OUTPUT_READY escalation arrives
- [ ] A refresh re-renders tasks, status, question and timeline from GET /tasks and GET /tasks/{id}/timeline
- [ ] No private API surface is used: only the documented endpoints
