# Nimbus Demo — Terraform Apply Checklist

> **WARNING: DO NOT APPLY UNTIL TOLD.**
> Do not run `terraform apply` (or `terraform plan` against live AWS if credentials are not yet finalized) until explicitly instructed for the live demo window. Running `apply` provisions an always-on ECS Fargate task that begins billing immediately.

## 1. Required AWS Credentials & Variables

Before running Terraform in `infra/terraform/`, configure your AWS credentials and Terraform input variables (`variables.tf`):

```bash
# Required AWS authentication (or use `aws configure` / AWS SSO profile)
export AWS_ACCESS_KEY_ID="<YOUR_AWS_ACCESS_KEY_ID>"
export AWS_SECRET_ACCESS_KEY="<YOUR_AWS_SECRET_ACCESS_KEY>"
export AWS_REGION="ap-south-1"

# Recommended Terraform variable overrides (TF_VAR_*)
export TF_VAR_aws_region="ap-south-1"
export TF_VAR_project_name="ds252"
export TF_VAR_app_image="<ACCOUNT_ID>.dkr.ecr.ap-south-1.amazonaws.com/nimbus-app:latest"
export TF_VAR_egress_mode="public_ip"
export TF_VAR_alert_email="<YOUR_TEAM_ALERT_EMAIL>"
export TF_VAR_budget_usd=50
```

Verify your active AWS identity before planning or applying:

```bash
aws sts get-caller-identity
```

## 2. Exact Commands for the Demo

Run all commands from the `infra/terraform/` directory:

```bash
cd infra/terraform

# 1. Initialize provider plugins (hashicorp/aws v5.100.0 locked in .terraform.lock.hcl)
terraform init

# 2. Validate HCL syntax and internal consistency offline
terraform validate

# 3. Generate and save an execution plan for review
terraform plan -out=demo.tfplan

# 4. Apply the saved plan (ONLY when instructed for the demo)
terraform apply "demo.tfplan"

# 5. Inspect outputs (API endpoint, SQS URL, S3 bucket, DynamoDB tables, IoT topics)
terraform output
```

When the demo concludes, tear down all resources immediately to stop ongoing Fargate charges:

```bash
terraform destroy
```

## 3. Main Resources Created

With default settings (`project_name = "ds252"`, prefix `nimbus-ds252`, `egress_mode = "public_ip"`), `main.tf` and `p3.tf` create the following 25 resources:

| Category | Terraform Resource | AWS Resource Name / Identifier | Purpose |
| :--- | :--- | :--- | :--- |
| **Networking** | `aws_vpc.main` | `nimbus-ds252-vpc` (`10.20.0.0/16`) | Core VPC with DNS hostnames enabled |
| **Networking** | `aws_subnet.private_a`, `aws_subnet.private_b` | `nimbus-ds252-private-a` (`10.20.1.0/24`), `nimbus-ds252-private-b` (`10.20.2.0/24`) | Subnets across `ap-south-1a` and `ap-south-1b` |
| **Networking** | `aws_internet_gateway.main[0]`, `aws_route_table.egress[0]`, `aws_route_table_association.egress_a[0]`, `aws_route_table_association.egress_b[0]` | `nimbus-ds252-igw` + route table (`0.0.0.0/0`) | Direct IGW egress when `egress_mode = "public_ip"` |
| **Compute (ECS)** | `aws_ecs_cluster.main` | `nimbus-ds252-cluster` | ECS cluster with Container Insights enabled |
| **Compute (ECS)** | `aws_ecs_task_definition.app` | `nimbus-ds252-app` | Fargate task (`512` CPU / 0.5 vCPU, `1024` MB RAM, port `8000`) |
| **Compute (ECS)** | `aws_ecs_service.app` | `nimbus-ds252-service` | Runs `desired_count = 1` Fargate task |
| **Logging** | `aws_cloudwatch_log_group.app` | `/ecs/nimbus-ds252` | Container logs with 30-day retention |
| **API Gateway** | `aws_apigatewayv2_api.main`, `aws_apigatewayv2_integration.placeholder`, `aws_apigatewayv2_route.requests` | `nimbus-ds252-api` (`POST /requests`) | HTTP API v2 front door (currently wired to `MOCK` integration) |
| **Messaging** | `aws_sqs_queue.escalations` | `nimbus-ds252-escalations` | SQS queue (`30s` visibility timeout) for escalations and check-ins |
| **Events** | `aws_cloudwatch_event_bus.main`, `aws_cloudwatch_event_rule.task_events`, `aws_cloudwatch_event_target.to_queue` | `nimbus-ds252-bus`, `nimbus-ds252-task-events` | Routes `nimbus.app` events (`lease_issued`, `escalation`, `checkin_missed`) to SQS |
| **DynamoDB** | `aws_dynamodb_table.leases`, `tasks`, `robot_status`, `decision_log`, `state` | `nimbus-ds252-leases`, `nimbus-ds252-tasks`, `nimbus-ds252-robot_status`, `nimbus-ds252-decision_log`, `nimbus-ds252-state` | 5 On-Demand (`PAY_PER_REQUEST`) tables; `state` enables TTL on `ttl` |
| **Storage** | `aws_s3_bucket.outputs` | `nimbus-ds252-outputs` | S3 bucket for task outputs (`force_destroy = false`) |
| **IAM** | `aws_iam_role.execution`, `aws_iam_role_policy_attachment.execution` | `nimbus-ds252-execution-role` | Attaches `AmazonECSTaskExecutionRolePolicy` for ECR pulls and logs |
| **IAM** | `aws_iam_role.task`, `aws_iam_role_policy.task_least_privilege`, `aws_iam_role_policy.task_p3` | `nimbus-ds252-task-role` | Grants access to DynamoDB, SQS, EventBridge, S3, Bedrock (`InvokeModel`), and IoT Core topics |
| **Cost Guard** | `aws_budgets_budget.monthly[0]` | `nimbus-ds252-monthly` | Monthly cost budget (created only when `alert_email` is non-empty) |

## 4. Cost-Relevant Notes

* **ECS Fargate (Primary Continuous Cost):** `aws_ecs_service.app` runs 1 always-on task (`0.5 vCPU`, `1 GB RAM`), which bills per second while running (~$0.025–$0.03/hour in `ap-south-1`). Also note that `Container Insights` is enabled on `aws_ecs_cluster.main`, which incurs CloudWatch custom metric ingestion charges while tasks are active.
* **NAT Gateway Avoidance:** `egress_mode` defaults to `"public_ip"`, routing task egress directly through an Internet Gateway (`aws_internet_gateway.main`) instead of a NAT Gateway, avoiding ~$32+/month in fixed NAT Gateway charges.
* **DynamoDB On-Demand (`PAY_PER_REQUEST`):** All 5 tables (`leases`, `tasks`, `robot_status`, `decision_log`, `state`) use on-demand billing with $0 idle compute cost; you pay only per read/write request unit and stored GB. The `state` table enables TTL (`ttl`) to expire stale leases and deduplication records automatically.
* **AWS IoT Core & Amazon Bedrock:** No standalone `aws_iot_*` thing or rule resources are created by Terraform; instead, `aws_iam_role_policy.task_p3` authorizes the Fargate task to publish/subscribe to IoT Core MQTT topics (`nimbus/f/+/telemetry`, `nimbus/f/+/status`, `nimbus/c/+/cmd`, `nimbus/f/+/evt`, `nimbus/c/broadcast/cmd`) and invoke Amazon Bedrock models (`bedrock:InvokeModel`). Both bill strictly per message/token used during the demo.
* **S3 Teardown Note:** `aws_s3_bucket.outputs` sets `force_destroy = false`. Empty the `nimbus-ds252-outputs` bucket before running `terraform destroy` if the demo uploads any objects.
* **Budget Alerts:** Setting `TF_VAR_alert_email` provisions `aws_budgets_budget.monthly` (default `$50/month`) with email alerts at **50%**, **80%**, and **100%** forecasted spend.
