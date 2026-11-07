# Cost Accounting, Telemetry, and AWS Billing Audit

**Target Cloud Environment:** AWS Account `930124786284` | Region `ap-south-1`  
**Reference Components:** `cost.py`, `server.py` (`CostBook`), `cloud/llm.py`, `infra/terraform/main.tf`, `results/final/N9/`

---

## 1. Runtime Cost Metering Methodology

Project Nimbus tracks runtime resource consumption via `CostBook` in `server.py` and computes task delivery costs against published AWS pricing tariffs.

### 1.1 Itemized Cost Formula
Per-task cost is computed under PRD Section 7 metrics:

$$\text{Cost}_{\text{task}} = \text{Cost}_{\text{Bedrock}} + \text{Cost}_{\text{Fargate}} + \text{Cost}_{\text{DynamoDB}} + \text{Cost}_{\text{APIGW}} + \text{Cost}_{\text{Transfer}}$$

| Resource Component | Metered Metric | AWS Tariff (ap-south-1) | Accounting Mechanism |
| :--- | :--- | :--- | :--- |
| **Amazon Bedrock (Nova-Lite)** | Input / Output Tokens | Input: \$0.06 / 1M tokens<br>Output: \$0.24 / 1M tokens | Token counts extracted from `usage` block returned by Converse API in `cloud/llm.py`. Failed/circuit-broken calls incur \$0.00. |
| **AWS ECS Fargate** | vCPU / GB Memory seconds | vCPU: \$0.04048 / hr<br>RAM: \$0.004445 / GB-hr | Computed across container allocation (0.5 vCPU, 1 GB RAM) divided by active task duration. |
| **Amazon DynamoDB** | Write / Read Request Units | \$1.25 / 1M WRU<br>\$0.25 / 1M RRU | `server.py` increments counters for every `put_task`, `append_decision`, `get_task`, and `query_tasks`. |
| **Amazon API Gateway** | HTTP API Requests | \$1.00 / 1M requests | Separated into **business task requests** (`POST /requests`, `POST /tasks/{id}/control`) and **background console polling** (`GET /tasks`, `GET /fleet`). |
| **Data Transfer** | Egress Bytes | \$0.09 / GB (first 10 TB free) | Monitored in `server.py:record(data_transfer_bytes=...)`. |

---

## 2. Infrastructure Spend vs Modelled Spend

> [!IMPORTANT]
> **Always-On Fargate & Observability Billing**:  
> ECS Fargate runs continuously at `desiredCount=1` (512 CPU, 1024 MB RAM), accumulating baseline infrastructure charges of approximately **\$0.51 / day** regardless of whether tasks are active. Container Insights and CloudWatch Logs accumulate additional storage and ingestion fees.
>
> If `var.budget_email` in Terraform is empty, automated AWS Budget threshold alert notifications are disabled. Teams must set `budget_email` in production to prevent unexpected overruns.

---

## 3. Safe Demo Teardown Checklist

When destroying or pausing the environment after evaluation to prevent ongoing billing:

1. **Empty Amazon S3 Artifact Bucket**:
   Non-empty S3 buckets will cause `terraform destroy` to fail with `BucketNotEmptyException`.
   ```bash
   aws s3 rm s3://nimbus-ds252-artifacts --recursive --region ap-south-1
   ```
2. **Delete ECR Images**:
   Non-empty ECR repositories may block deletion unless `force_delete = true` is configured.
   ```bash
   aws ecr batch-delete-image --repository-name nimbus-app --image-ids imageTag=latest --region ap-south-1
   ```
3. **Scale ECS Service to Zero (Pause Mode)**:
   If temporary pause is desired without destroying Terraform state:
   ```bash
   aws ecs update-service --cluster nimbus-ds252-cluster --service nimbus-ds252-service --desired-count 0 --region ap-south-1
   ```
4. **Terraform Destroy**:
   Run full infrastructure teardown from `infra/terraform/`:
   ```bash
   terraform -chdir=infra/terraform destroy -auto-approve
   ```
