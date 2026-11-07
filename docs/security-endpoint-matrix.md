# Security Endpoint Matrix: Nimbus P4 API Authorization

**Scope:** Complete HTTP ingress surface implemented in `server.py` on AWS ECS Fargate (`nimbus-ds252-service`) and API Gateway (`qrgdidp9te`).  
**Auth Implementation:** Fail-closed bearer token and `X-Nimbus-Token` validation (`server.py:_is_authorized`).  
**Bypass Mechanism:** `NIMBUS_AUTH_DISABLED=1` strictly for local testing; fails closed when unset in production.

---

## 1. Role Definitions and Token Hierarchies

| Role | Environment Variable | Permissions & Scopes | Allowed Methods |
| :--- | :--- | :--- | :--- |
| **Public** | *None* | Unauthenticated static assets, UI landing pages, health checks, schemas, public pack info | `GET` |
| **Requester** | `NIMBUS_REQUESTER_TOKEN` | Submit natural language requests, answer clarifications, inspect own task status | `GET`, `POST` |
| **Operator** | `NIMBUS_OPERATOR_TOKEN` | Telemetry inspection, inventory adjustments, task controls (pause/cancel/replan), corridor reopen, memory reset | `GET`, `POST` |
| **Edge** | `NIMBUS_EDGE_TOKEN` | Robot check-ins, edge escalations (`CLARIFY`, `UNEXPECTED_EVENT`, `OUTPUT_READY`), fleet heartbeats, relay decisions | `POST` |
| **Admin / Fault** | `NIMBUS_ADMIN_TOKEN` / `NIMBUS_FAULT_TOKEN` | Network fault simulation injection, LLM outage simulation toggling | `GET`, `POST` |
| **Master Superuser** | `NIMBUS_AUTH_TOKEN` | Universal fallback token for automated deployment pipelines and E2E harnesses | All |

---

## 2. Complete Endpoint Inventory & Authorization Matrix

| Endpoint Route | HTTP Method | Required Role | Auth Mechanism | Current Protection Status | Automated Test Reference |
| :--- | :---: | :--- | :--- | :---: | :--- |
| `/` | `GET` | **Public** | Unauthenticated | Allowed | `test_p4_security.py::test_public_routes_accessible_without_auth` |
| `/app.js` | `GET` | **Public** | Unauthenticated | Allowed | `test_p4_security.py::test_public_routes_accessible_without_auth` |
| `/operator` | `GET` | **Public** | Unauthenticated (UI Frame) | Allowed | `test_api.py::test_operator_page_served` |
| `/operator.js` | `GET` | **Public** | Unauthenticated | Allowed | `test_api.py::test_operator_page_served` |
| `/map` / `/map.html` | `GET` | **Public** | Unauthenticated | Allowed | `test_p4_security.py::test_public_routes_accessible_without_auth` |
| `/map.json` | `GET` | **Public** | Unauthenticated | Allowed | `test_p4_security.py::test_public_routes_accessible_without_auth` |
| `/pack` | `GET` | **Public** | Unauthenticated | Allowed | `test_p4_security.py::test_public_routes_accessible_without_auth` |
| `/ui-config` | `GET` | **Public** | Unauthenticated | Allowed | `test_p4_security.py::test_public_routes_accessible_without_auth` |
| `/health` | `GET` | **Public** | Unauthenticated | Allowed | `test_p4_security.py::test_public_routes_accessible_without_auth` |
| `/schemas/{schema}` | `GET` | **Public** | Unauthenticated | Allowed | `test_api.py::test_schemas_served_over_http` |
| `/tasks/{id}` | `GET` | **Requester / Operator** | Unauthenticated for single task status / Token permitted | Allowed (Read-only status) | `test_cloud_api.py::test_sc1_task_view_shows_spec_and_priority` |
| `/tasks` | `GET` | **Operator** | `Bearer <NIMBUS_OPERATOR_TOKEN>` | Protected (401 on unset/invalid) | `test_p4_security.py::test_fail_closed_when_auth_enabled_and_tokens_unset` |
| `/fleet` | `GET` | **Operator** | `Bearer <NIMBUS_OPERATOR_TOKEN>` | Protected (401 on unset/invalid) | `test_p4_security.py::test_token_authorization_and_role_separation` |
| `/outbox` | `GET` | **Operator** | `Bearer <NIMBUS_OPERATOR_TOKEN>` | Protected (401 on unset/invalid) | `test_p4_security.py::test_fail_closed_when_auth_enabled_and_tokens_unset` |
| `/trace` | `GET` | **Operator** | `Bearer <NIMBUS_OPERATOR_TOKEN>` | Protected (401 on unset/invalid) | `test_p4_security.py::test_fail_closed_when_auth_enabled_and_tokens_unset` |
| `/notifications` | `GET` | **Operator** | `Bearer <NIMBUS_OPERATOR_TOKEN>` | Protected (401 on unset/invalid) | `test_p4_security.py::test_fail_closed_when_auth_enabled_and_tokens_unset` |
| `/cost` | `GET` | **Operator** | `Bearer <NIMBUS_OPERATOR_TOKEN>` | Protected (401 on unset/invalid) | `test_p4_security.py::test_fail_closed_when_auth_enabled_and_tokens_unset` |
| `/metrics` | `GET` | **Operator** | `Bearer <NIMBUS_OPERATOR_TOKEN>` | Protected (401 on unset/invalid) | `test_p4_security.py::test_fail_closed_when_auth_enabled_and_tokens_unset` |
| `/inventory` | `GET` | **Operator** | `Bearer <NIMBUS_OPERATOR_TOKEN>` | Protected (401 on unset/invalid) | `test_p4_security.py::test_fail_closed_when_auth_enabled_and_tokens_unset` |
| `/llm-status` | `GET` | **Operator** | `Bearer <NIMBUS_OPERATOR_TOKEN>` | Protected (401 on unset/invalid) | `test_p4_security.py::test_fail_closed_when_auth_enabled_and_tokens_unset` |
| `/requests` | `POST` | **Requester** | `Bearer <NIMBUS_REQUESTER_TOKEN>` | Protected (401 on unset/invalid) | `test_p4_security.py::test_token_authorization_and_role_separation` |
| `/requests/{id}/answer` | `POST` | **Requester** | `Bearer <NIMBUS_REQUESTER_TOKEN>` | Protected (401 on unset/invalid) | `test_p4_security.py::test_token_authorization_and_role_separation` |
| `/tasks/{id}/control` | `POST` | **Operator** | `Bearer <NIMBUS_OPERATOR_TOKEN>` | Protected (401 on unset/invalid) | `test_p4_security.py::test_token_authorization_and_role_separation` |
| `/tasks/{id}/replan` | `POST` | **Operator** | `Bearer <NIMBUS_OPERATOR_TOKEN>` | Protected (401 on unset/invalid) | `test_p4_security.py::test_token_authorization_and_role_separation` |
| `/map/reopen` | `POST` | **Operator** | `Bearer <NIMBUS_OPERATOR_TOKEN>` | Protected (401 on unset/invalid) | `test_p4_security.py::test_fail_closed_when_auth_enabled_and_tokens_unset` |
| `/inventory/stock` / `/fleet/stock` | `POST` | **Operator** | `Bearer <NIMBUS_OPERATOR_TOKEN>` | Protected (401 on unset/invalid) | `test_p4_security.py::test_fail_closed_when_auth_enabled_and_tokens_unset` |
| `/inventory/reset` | `POST` | **Operator** | `Bearer <NIMBUS_OPERATOR_TOKEN>` | Protected (401 on unset/invalid) | `test_p4_security.py::test_fail_closed_when_auth_enabled_and_tokens_unset` |
| `/reset-memory` / `/memory/reset` | `POST` | **Operator** | `Bearer <NIMBUS_OPERATOR_TOKEN>` | Protected (401 on unset/invalid) | `test_p4_security.py::test_fail_closed_when_auth_enabled_and_tokens_unset` |
| `/checkins` | `POST` | **Edge** | `Bearer <NIMBUS_EDGE_TOKEN>` | Protected (401 on unset/invalid) | `test_p4_security.py::test_fail_closed_when_auth_enabled_and_tokens_unset` |
| `/escalations` | `POST` | **Edge** | `Bearer <NIMBUS_EDGE_TOKEN>` | Protected (401 on unset/invalid) | `test_p4_security.py::test_fail_closed_when_auth_enabled_and_tokens_unset` |
| `/fleet/heartbeat` | `POST` | **Edge** | `Bearer <NIMBUS_EDGE_TOKEN>` | Protected (401 on unset/invalid) | `test_p4_security.py::test_fail_closed_when_auth_enabled_and_tokens_unset` |
| `/relay/decide` | `POST` | **Edge** | `Bearer <NIMBUS_EDGE_TOKEN>` | Protected (401 on unset/invalid) | `test_p4_security.py::test_fail_closed_when_auth_enabled_and_tokens_unset` |
| `/fault` | `GET`, `POST` | **Admin / Fault** | `Bearer <NIMBUS_ADMIN_TOKEN>` or `NIMBUS_DEMO_FAULTS=1` | Protected (403 on unauth) | `test_api.py::test_fault_endpoint_auth_guard` |
| `/llm/outage` | `POST` | **Admin / Fault** | `Bearer <NIMBUS_ADMIN_TOKEN>` or `NIMBUS_DEMO_FAULTS=1` | Protected (403 on unauth) | `test_api.py::test_fault_endpoint_auth_guard` |

---

## 3. Security Properties & Hardening Details

1. **Fail-Closed Default (C1)**: If an authorization token for a role is unset in the environment, requests targeting that role receive HTTP 401 Unauthorized immediately. They never proceed with unauthenticated execution.
2. **Timing Attack Protection**: Token evaluation uses `hmac.compare_digest` in `server.py` to prevent timing-based side-channel discovery of secret tokens.
3. **Credential Non-Leakage**: Error responses strictly return generic sanitized error messages: `{"error": "unauthorized", "detail": "Missing authorization token"}`. Token hashes, internal system env keys, or secret substrings are never echoed in HTTP error payloads or access logs.
4. **Direct Fargate vs API Gateway Protection**: Authentication is enforced directly inside the application container (`server.py`), guaranteeing that even if a request bypasses API Gateway and reaches the Fargate container directly over VPC, it is rejected with 401.
5. **Payload Size Guard (Addendum 9)**: `server.py:_read_json` rejects any payload with `Content-Length > 10 MB` with `HTTP 400 Bad Request`, preventing memory exhaustion DOS attacks.
