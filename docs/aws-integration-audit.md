# AWS Infrastructure and Service Integration Audit

**Target Environment:** AWS Account `930124786284` | Region `ap-south-1`  
**ECS Service:** `nimbus-ds252-service` (`nimbus-ds252-cluster`)  
**Terraform Base:** `infra/terraform/main.tf`, `infra/terraform/variables.tf`  
**Audit Status:** P4 Red-Team Remediation Complete (C2, C3, C4, Addendum 2)

---

## 1. AWS IoT Core Integration Audit

| Property | Application Design | Terraform Implementation | Status / Finding |
| :--- | :--- | :--- | :--- |
| **MQTT Client** | `PahoMQTTClient` in `mqtt_transport.py` | Configured via `var.iot_endpoint` | Clean fallback on connect timeout |
| **Subscribed Topics** | `nimbus/+/escalation`<br>`nimbus/+/checkin` | IoT Policy allows topic filters `arn:aws:iot:ap-south-1:930124786284:topicfilter/nimbus/*` | Validated; wildcard topicfilter aligns with single-level `+` in Paho |
| **Published Topics** | `nimbus/{cluster_id}/lease`<br>`nimbus/{cluster_id}/heartbeat` | IoT Policy allows `arn:aws:iot:ap-south-1:930124786284:topic/nimbus/*` | Validated; exact topic publishing permitted |
| **Client Authentication**| X.509 Certificate mutual TLS / AWS SigV4 | IoT Thing Policy attached to container certificate | Required for physical edge robots; disabled by default in synthetic runs (`NIMBUS_MQTT_DISABLED=1`) |
| **Failure Tolerance** | Non-blocking background connect; non-fatal if broker is absent | Application logs warning and operates over HTTP ingress if MQTT is unreachable | **Fixed (H5)**: Startup crash eliminated |

---

## 2. Amazon SQS Integration Audit

| Property | Configuration | Code Reference | Audit Findings |
| :--- | :--- | :--- | :--- |
| **Queue Name** | `nimbus-ds252-escalations` | `cloud/aws_sqs.py` / `infra/terraform/main.tf` | Standard queue provisioned |
| **Visibility Timeout** | 300 seconds | AWS SDK default overridden to 300s | SQS visibility timeout exceeds maximum Bedrock LLM round-trip timeout (15s) and edge decision loop (30s), preventing premature duplicate redelivery |
| **Dead-Letter Queue** | `nimbus-ds252-escalations-dlq` | `redrive_policy` (maxReceiveCount = 5) | Prevents poison message loops from exhausting ECS CPU |
| **IAM Permissions** | `sqs:SendMessage`, `sqs:ReceiveMessage`, `sqs:DeleteMessage`, `sqs:GetQueueAttributes` | Task execution IAM policy | Scoped strictly to queue ARN `arn:aws:sqs:ap-south-1:930124786284:nimbus-ds252-escalations*` |

---

## 3. Amazon EventBridge Integration Audit

| Property | Application Status | Audit Evaluation | Recommendation / Ownership |
| :--- | :--- | :--- | :--- |
| **Event Bus** | `default` bus rule defined in Terraform | No active `boto3.client("events").put_events` calls found in P4 runtime code (`server.py`) | **P3 Ownership Handoff**: Cloud orchestrator currently records decision log entries directly to DynamoDB rather than publishing to EventBridge. The Terraform rule is retained for future decoupled analytics, but not relied upon for core task leasing. |

---

## 4. IAM Least-Privilege Audit

| IAM Action Set | Resource Scope | Audit Finding | Remediation Applied |
| :--- | :--- | :--- | :--- |
| `dynamodb:GetItem`<br>`dynamodb:PutItem`<br>`dynamodb:UpdateItem`<br>`dynamodb:DeleteItem`<br>`dynamodb:Query`<br>`dynamodb:Scan` | `arn:aws:dynamodb:ap-south-1:930124786284:table/nimbus-ds252-*` | Scoped to the 5 project tables | Least privilege verified |
| `dynamodb:TransactWriteItems`<br>`dynamodb:TransactGetItems`<br>`dynamodb:DescribeTable` | `arn:aws:dynamodb:ap-south-1:930124786284:table/nimbus-ds252-*` | Previously missing in Terraform policy; led to runtime permission errors on atomic lease increments | **Fixed (C4)**: Explicitly added to `aws_iam_policy.app_dynamo` |
| `bedrock:InvokeModel` | `arn:aws:bedrock:ap-south-1::foundation-model/apac.amazon.nova-lite-v1:0` | Scoped strictly to Nova-Lite regional model ARN | Prevents unauthorized invocation of expensive Anthropic/Titan models |
| `s3:PutObject`<br>`s3:GetObject` | `arn:aws:s3:::nimbus-ds252-artifacts/*` | Scoped strictly to the project artifact bucket prefix | No wildcard bucket access |

---

## 5. Secrets Manager & Environment Configuration Audit

- **No Plaintext Secrets in Repository**: Full git history scan confirmed 0 AWS secret keys, access tokens, or private credentials committed.
- **Secure ECS Environment Ingestion**: Environment variables in `infra/terraform/main.tf` reference `NIMBUS_AUTH_TOKEN`, `NIMBUS_EDGE_TOKEN`, `NIMBUS_OPERATOR_TOKEN` via SSM Parameter Store / Secrets Manager references.
- **Fail-Closed Runtime**: If tokens are not provided, `server.py` rejects mutating endpoints with `HTTP 401 Unauthorized` rather than failing open.

---

## 6. Amazon S3 and CloudFront Audit

- **Artifact Bucket (`nimbus-ds252-artifacts`)**:
  - `aws_s3_bucket_public_access_block`: All 4 public access block flags (`block_public_acls`, `ignore_public_acls`, `block_public_policy`, `restrict_public_buckets`) are set to `true`.
  - Server-Side Encryption: AES-256 (`aws:kms` or `AES256`) enforced on all PUT operations.
  - Bucket Versioning: Enabled to guarantee immutable historical preservation of task result payloads.
- **CloudFront CDN**: Not deployed in the production Terraform configuration. Static UI assets (`static/requester.html`, `static/operator.html`, `static/app.js`, `static/operator.js`) are served directly by the Python HTTP server via API Gateway HTTP API proxy, eliminating CDN synchronization lag.

---

## 7. Amazon CloudWatch Metrics and Alarms Alignment

- **Log Group**: `/ecs/nimbus-ds252-app` with 30-day retention.
- **ECS Alarms**:
  - `CPUUtilization > 80%` for 2 consecutive periods of 60 seconds.
  - `MemoryUtilization > 85%` for 2 consecutive periods of 60 seconds.
- **Application Health Metric**: Heartbeat misses are detected via `missed_checkin` alarm. Metric namespace matches `Nimbus/Fleet` and dimension `ClusterId` emitted by `cloud/orchestrator.py`. Duplicate skeleton alarm was removed (**C3**).
- **Public Ingress & Security Group (C2)**: Ingress to port 8000 is restricted to `var.allowed_ingress_cidr_blocks` (`["10.20.0.0/16"]`), and `assign_public_ip` is set to `false` by default, eliminating direct Internet exposure.
