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
  description = "Container image for the always-on Fargate service. Person 3 sets the real ECR image."
  type        = string
  default     = "ghcr.io/team-nimbus/nimbus-app:placeholder"
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
