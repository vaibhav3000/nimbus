# 05: Terraform skeleton (validated, never applied)

**What to build:** The AWS deployment skeleton as Terraform: VPC placeholder, ECS Fargate
always-on service, API Gateway with a placeholder integration, SQS, EventBridge,
DynamoDB tables (leases, tasks, robot_status, decision_log), S3 bucket for outputs, IoT
topic names, least-privilege IAM. No Lambda. `terraform validate` passes locally; nothing
is ever applied or planned against a real account.

**Blocked by:** None (can start immediately; table/queue/topic names follow the contracts).

**Status:** ready-for-agent

- [ ] All scoped resources present, names aligned with the contracts' vocabulary; IoT topic names declared as locals
- [ ] No Lambda resource anywhere; ECS service is always-on Fargate
- [ ] IAM policies are scoped to specific table, queue, bucket-prefix, Bedrock-invoke and IoT ARNs, plus a minimal execution role
- [ ] `terraform init -backend=false` + `terraform validate` exit 0 with the checked-in provider lock
- [ ] A comment marks the placeholder integration for Person 3 to replace with a VPC-link proxy
