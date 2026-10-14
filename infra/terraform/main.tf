# Nimbus: planned AWS deployment skeleton (PRD v5.0 Fig 4).
#
# SKELETON ONLY: `terraform validate` passes; never apply without the team's
# AWS account owner. Person 3 extends this with the real orchestrator wiring.
#
# No Lambda anywhere (PRD Fig 4): check-ins arrive every few seconds from every
# cluster, so the deployment uses always-on containers on ECS Fargate instead
# of one function call per message.

terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

locals {
  name_prefix = "nimbus-${var.project_name}"

  # Cloud-edge messaging (PRD Fig 4, component 6/7). One topic family per edge
  # cluster; the edge subscribes to its own topics only (FR-11 selective sharing).
  iot_topics = {
    leases      = "nimbus/${var.project_name}/leases/+"
    escalations = "nimbus/${var.project_name}/escalations/+"
    checkins    = "nimbus/${var.project_name}/checkins/+"
  }
}

# ---------------------------------------------------------------------------
# Network placeholder: one VPC, two private subnets for the Fargate service.
# Person 3: add NAT gateways / VPC endpoints (Bedrock, S3, DynamoDB) when the
# real deployment is sized.
# ---------------------------------------------------------------------------

resource "aws_vpc" "main" {
  cidr_block           = "10.20.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "${local.name_prefix}-vpc" }
}

resource "aws_subnet" "private_a" {
  vpc_id            = aws_vpc.main.id
  cidr_block        = "10.20.1.0/24"
  availability_zone = "${var.aws_region}a"

  tags = { Name = "${local.name_prefix}-private-a" }
}

resource "aws_subnet" "private_b" {
  vpc_id            = aws_vpc.main.id
  cidr_block        = "10.20.2.0/24"
  availability_zone = "${var.aws_region}b"

  tags = { Name = "${local.name_prefix}-private-b" }
}

# ---------------------------------------------------------------------------
# ECS Fargate: always-on containers (orchestrator, scheduler, lease manager,
# tool service, health monitor in one service for the skeleton; Person 3 may
# split them into separate task definitions without schema changes).
# ---------------------------------------------------------------------------

resource "aws_ecs_cluster" "main" {
  name = "${local.name_prefix}-cluster"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/${local.name_prefix}"
  retention_in_days = 30
}

resource "aws_ecs_task_definition" "app" {
  family                   = "${local.name_prefix}-app"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([
    {
      name      = "nimbus-app"
      image     = var.app_image
      essential = true
      portMappings = [
        { containerPort = 8000, protocol = "tcp" }
      ]
      environment = [
        { name = "AWS_REGION", value = var.aws_region },
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.app.name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "app"
        }
      }
    }
  ])
}

resource "aws_ecs_service" "app" {
  name            = "${local.name_prefix}-service"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.app.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = [aws_subnet.private_a.id, aws_subnet.private_b.id]
    assign_public_ip = var.egress_mode == "public_ip" # see p3.tf: private subnets had no route out
  }
}

# ---------------------------------------------------------------------------
# API Gateway (HTTP API) in front of the service. PLACEHOLDER integration:
# Person 3 replaces the MOCK integration with a VPC-link proxy (or an internal
# ALB) to the Fargate service once networking is sized.
# ---------------------------------------------------------------------------

resource "aws_apigatewayv2_api" "main" {
  name          = "${local.name_prefix}-api"
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "placeholder" {
  api_id                 = aws_apigatewayv2_api.main.id
  integration_type       = "MOCK"
  integration_uri        = "placeholder"
  payload_format_version = "1.0"
}

resource "aws_apigatewayv2_route" "requests" {
  api_id    = aws_apigatewayv2_api.main.id
  route_key = "POST /requests"
  target    = "integrations/${aws_apigatewayv2_integration.placeholder.id}"
}

# ---------------------------------------------------------------------------
# Queues, timers, tables, storage (PRD Fig 4 components 4, 5, 7).
# ---------------------------------------------------------------------------

resource "aws_sqs_queue" "escalations" {
  name                       = "${local.name_prefix}-escalations"
  visibility_timeout_seconds = 30
}

resource "aws_cloudwatch_event_bus" "main" {
  name = "${local.name_prefix}-bus"
}

resource "aws_cloudwatch_event_rule" "task_events" {
  name        = "${local.name_prefix}-task-events"
  description = "Route Nimbus task/lease events to the escalations queue"
  event_pattern = jsonencode({
    source      = ["nimbus.app"]
    detail-type = ["lease_issued", "escalation", "checkin_missed"]
  })
}

resource "aws_cloudwatch_event_target" "to_queue" {
  event_bus_name = aws_cloudwatch_event_bus.main.name
  rule           = aws_cloudwatch_event_rule.task_events.name
  arn            = aws_sqs_queue.escalations.arn
}

resource "aws_dynamodb_table" "leases" {
  name         = "${local.name_prefix}-leases"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "lease_id"

  attribute {
    name = "lease_id"
    type = "S"
  }
}

resource "aws_dynamodb_table" "tasks" {
  name         = "${local.name_prefix}-tasks"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "task_id"

  attribute {
    name = "task_id"
    type = "S"
  }
}

resource "aws_dynamodb_table" "robot_status" {
  name         = "${local.name_prefix}-robot_status"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "robot_id"

  attribute {
    name = "robot_id"
    type = "S"
  }
}

resource "aws_dynamodb_table" "decision_log" {
  name         = "${local.name_prefix}-decision_log"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "task_id"
  range_key    = "ts"

  attribute {
    name = "task_id"
    type = "S"
  }
  attribute {
    name = "ts"
    type = "S"
  }
}

resource "aws_s3_bucket" "outputs" {
  bucket        = "${local.name_prefix}-outputs"
  force_destroy = false
}

# ---------------------------------------------------------------------------
# IAM: least privilege (NFR-5). The task role can touch exactly the tables,
# queue, bucket and topics created above, plus Bedrock invoke. No wildcards
# on resources except the Bedrock foundation-model set, which is region-scoped.
# ---------------------------------------------------------------------------

resource "aws_iam_role" "task" {
  name = "${local.name_prefix}-task-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "task_least_privilege" {
  name = "${local.name_prefix}-task-policy"
  role = aws_iam_role.task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Sid    = "Tables"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
          "dynamodb:DeleteItem", "dynamodb:Query",
        ]
        Resource = [
          aws_dynamodb_table.leases.arn,
          aws_dynamodb_table.tasks.arn,
          aws_dynamodb_table.robot_status.arn,
          aws_dynamodb_table.decision_log.arn,
        ]
      },
      {
        Sid      = "Queue"
        Effect   = "Allow"
        Action   = ["sqs:SendMessage", "sqs:ReceiveMessage"]
        Resource = aws_sqs_queue.escalations.arn
      },
      {
        Sid      = "OutputsBucket"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = "${aws_s3_bucket.outputs.arn}/*"
      },
      {
        Sid      = "EventBus"
        Effect   = "Allow"
        Action   = ["events:PutEvents"]
        Resource = aws_cloudwatch_event_bus.main.arn
      },
      {
        Sid      = "BedrockInvoke"
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel"]
        Resource = "arn:aws:bedrock:${var.aws_region}::foundation-model/*"
      },
      {
        Sid    = "IotTopics"
        Effect = "Allow"
        Action = ["iot:Connect", "iot:Publish", "iot:Subscribe"]
        Resource = concat(
          [for pattern in values(local.iot_topics) : "arn:aws:iot:${var.aws_region}:*:topic/${pattern}"],
          ["arn:aws:iot:${var.aws_region}:*:client/${local.name_prefix}-*"]
        )
      },
    ])
  })
}

resource "aws_iam_role" "execution" {
  name = "${local.name_prefix}-execution-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}
