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
