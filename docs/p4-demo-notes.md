# Nimbus P4 Infrastructure & Live Demo Notes

This document describes the AWS infrastructure and deployment architecture designed for Nimbus (PRD v5.0 Fig. 4, Components 1–10), clarifying what is declared and validated in Terraform versus what requires a real AWS apply and runtime provisioning.

---

## 1. Validated Terraform Declarations (`terraform validate` Passing)

All resources below are fully declared in `infra/terraform/` (`main.tf`, `p3.tf`, `variables.tf`, `outputs.tf`) and pass automated syntax and dependency validation:

### Network & Compute (VPC & ECS Fargate)
- **VPC & Subnets**: Single-region VPC (`10.20.0.0/16`) in `ap-south-1` (Mumbai) across two Availability Zones (`ap-south-1a`, `ap-south-1b`).
- **Egress Routing**: Internet Gateway and route tables enabling task internet egress (ECR image pulls, AWS Bedrock API calls) under `egress_mode = "public_ip"` without incurring monthly NAT Gateway base charges.
- **Security Group**: Explicit `aws_security_group.app` allowing inbound TCP port 8000 for the Nimbus web service and unrestricted outbound traffic.
- **ECS Fargate Cluster & Service**: Always-on ECS Fargate service (`cpu = 512`, `memory = 1024`) with Container Insights enabled and CloudWatch Logs retention (30 days).
- **ECR Repository**: `aws_ecr_repository.app` with `force_delete = true` (enabling clean `terraform destroy`) and automated lifecycle rules (retaining 10 recent images).

### API & Ingress
- **API Gateway HTTP API**: `aws_apigatewayv2_api.main` configured with an `$default` auto-deploying stage.
- **HTTP_PROXY Integration**: Real HTTP proxy integration (`aws_apigatewayv2_integration.app`) targeting `var.ecs_target_url`.
- **Routes**: `POST /requests`, `ANY /{proxy+}`, and `GET /` proxying incoming HTTP traffic directly to the containerized Nimbus backend.

### Data & Messaging
- **DynamoDB State Tables**:
  - `leases`: Primary key `lease_id`.
  - `tasks`: Primary key `task_id`.
  - `robot_status`: Primary key `robot_id`.
  - `decision_log`: Composite key `task_id` (hash) and `ts` (range).
  - `state`: P3 state table for lease pointers, robot/zone claims, deduplication IDs, and TTL expiration on `ttl`.
- **SQS Escalations Queue**: `aws_sqs_queue.escalations` for edge-to-cloud escalations and missed check-ins.
- **EventBridge Event Bus & Rules**: `aws_cloudwatch_event_bus.main` routing `nimbus.app` task/lease events to SQS.
- **S3 Outputs Bucket**: `aws_s3_bucket.outputs` for task execution artifacts, bundles, and logs.

### AWS IoT Core & Static Distribution Skeletons
- **AWS IoT Policy**: `aws_iot_policy.edge_cluster` granting edge clusters permissions to connect and communicate across the five canonical topics:
  - Publish: `nimbus/*/escalation`, `nimbus/*/checkin`, `nimbus/*/probe`
  - Subscribe & Receive: `nimbus/*/lease`, `nimbus/*/renewal`
- **S3 Static Bucket & CloudFront**: `aws_s3_bucket.static_app`, `aws_cloudfront_origin_access_control.static_app` (OAC), and `aws_cloudfront_distribution.static_app` declared with `enabled = false` (safe skeleton that avoids premature global distribution rollout costs).
- **Secrets Manager**: `aws_secretsmanager_secret.app_secrets` skeleton container with least-privilege IAM read permissions attached to the ECS task role.
- **CloudWatch Metric Alarm**: `aws_cloudwatch_metric_alarm.missed_checkin` monitoring `Nimbus/App MissedCheckins` emitted via CloudWatch Embedded Metric Format (EMF).
- **Cost Guard**: AWS Budgets monthly threshold alerts at 50%, 80%, and 100% of budgeted amount (`var.budget_usd`).

---

## 2. Deploy-Time Requirements (Requires Live AWS Apply + Account Ownership)

The following steps are runtime deployment operations that cannot be performed in local offline validation:

1. **ECR Image Push**:
   - Building the container image via Docker: `docker build -t nimbus-app .`
   - Authenticating to ECR: `aws ecr get-login-password --region ap-south-1 | docker login ...`
   - Pushing the image tag to `aws_ecr_repository.app.repository_url`.
2. **ECS Public IP / Load Balancer Target**:
   - The ECS Fargate task acquires a dynamic public IP upon launch when `assign_public_ip = true`.
   - For a production deployment, an Application Load Balancer (ALB) or fixed IP target must be passed via `var.ecs_target_url` so API Gateway routes to the active task.
3. **AWS IoT Device Onboarding**:
   - In accordance with AWS IoT security best practices, individual AWS IoT Things and X.509 device certificates are generated per physical/simulated edge cluster during device provisioning.
   - The generated certificates are attached to `aws_iot_policy.edge_cluster`.
4. **Secrets Manager Value Population**:
   - Actual API credentials (e.g. Bedrock cross-account credentials, external webhook secrets) are populated into `aws_secretsmanager_secret.app_secrets` using `aws secretsmanager put-secret-value`.
5. **CloudFront Static Site Activation**:
   - Enabling the CloudFront distribution (`enabled = true`) and syncing UI assets (`static/`) to `aws_s3_bucket.static_app`.
   - CloudFront edge distribution propagation typically takes 5–15 minutes across AWS edge locations.
6. **Bedrock Model Access**:
   - Verifying that model invocation access for `apac.amazon.nova-lite-v1:0` (or `anthropic.claude-3-haiku`) is granted in the AWS account and region.

---

## 3. Transport Matrix for Demonstrations

| Mode | Transport Backend | State Backend | How to Run |
|---|---|---|---|
| **Local Demo (Default)** | HTTP REST + Local JSONL | Filesystem (`store/`) | `python server.py` (Default port `8000`) |
| **MQTT Simulation** | MQTT (5 Canonical Topics) | In-memory / SQLite | Set `NIMBUS_TRANSPORT=mqtt` with local Mosquitto broker |
| **AWS Cloud (Live)** | HTTP API Gateway + SQS/EventBridge | DynamoDB + S3 | Deployed on ECS Fargate via Terraform with `NIMBUS_BACKEND=aws` |
| **AWS IoT Core** | AWS IoT Core MQTT Broker (mTLS) | DynamoDB + State Table | Edge robots use X.509 certs against AWS IoT endpoint |
