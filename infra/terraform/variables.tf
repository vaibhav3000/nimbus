variable "aws_region" {
  description = "Single AWS region (PRD Sec. 2: AWS, single region, Terraform)."
  type        = string
  default     = "ap-south-1" # Mumbai; IISc is in Bengaluru
}

variable "project_name" {
  description = "Team project slug used in every resource name."
  type        = string
  default     = "ds252"
}

variable "app_image" {
  description = "Container image for the Fargate service. Empty (the default) uses the ECR repository this configuration creates, tag `latest`; push it with scripts/build_push.sh."
  type        = string
  default     = ""
}

variable "egress_mode" {
  description = "How the Fargate task reaches the internet (image pull, Bedrock): public_ip (cheapest) or none (supply VPC endpoints yourself)."
  type        = string
  default     = "public_ip"

  validation {
    condition     = contains(["public_ip", "none"], var.egress_mode)
    error_message = "egress_mode must be public_ip or none."
  }
}

variable "alert_email" {
  description = "E-mail for budget alerts. Empty disables the budget."
  type        = string
  default     = ""
}

variable "budget_usd" {
  description = "Monthly budget in USD that the 50/80/100% alerts are measured against."
  type        = number
  default     = 50
}

variable "bedrock_model_id" {
  description = "Bedrock model or inference-profile id the orchestrator calls (NIMBUS_BEDROCK_MODEL)."
  type        = string
  default     = "apac.amazon.nova-lite-v1:0"
}

variable "nimbus_mode" {
  description = "Evaluation configuration the service runs: C1 (proposed), C2 (cloud-only) or C3 (no LLM)."
  type        = string
  default     = "C1"
}

variable "knowledge_base_id" {
  description = "Bedrock Knowledge Base id for rule retrieval (FR-19). Empty keeps keyword retrieval from the pack."
  type        = string
  default     = ""
}

variable "ecs_target_url" {
  description = "Target HTTP URL for the ECS Fargate service / container proxy. Can be set to a task public IP, internal ALB, or mock URL at apply time."
  type        = string
  default     = "http://127.0.0.1:8000"
}

variable "assign_public_ip" {
  description = "Whether to assign public IP to Fargate tasks. Defaults to false for security; API Gateway or internal load balancer should be used for access."
  type        = bool
  default     = false
}

variable "allowed_ingress_cidr_blocks" {
  description = "Allowed CIDR blocks for inbound port 8000 on ECS security group. Restricted to internal VPC CIDR by default; public traffic enters via API Gateway."
  type        = list(string)
  default     = ["10.20.0.0/16"]
}
