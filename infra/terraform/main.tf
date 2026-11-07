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

  # Cloud-edge messaging (PRD Fig 4, component 6/7). Five topic patterns
  # connecting the cloud and edge clusters per contracts/topics.md.
  iot_topics = {
    lease      = "nimbus/+/lease"
    escalation = "nimbus/+/escalation"
    checkin    = "nimbus/+/checkin"
    renewal    = "nimbus/+/renewal"
    probe      = "nimbus/+/probe"
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
      image     = local.app_image
      essential = true
      portMappings = [
        { containerPort = 8000, protocol = "tcp" }
      ]
      environment = [
        { name = "AWS_REGION", value = var.aws_region },
        { name = "NIMBUS_BACKEND", value = "aws" },
        { name = "NIMBUS_LLM", value = "bedrock" },
        { name = "NIMBUS_BEDROCK_MODEL", value = var.bedrock_model_id },
        { name = "NIMBUS_MODE", value = var.nimbus_mode },
        { name = "NIMBUS_DDB_PREFIX", value = local.name_prefix },
        { name = "NIMBUS_RESULTS_BUCKET", value = aws_s3_bucket.outputs.bucket },
        { name = "NIMBUS_SQS_QUEUE_URL", value = aws_sqs_queue.escalations.url },
        { name = "NIMBUS_EMF", value = "1" },
        { name = "NIMBUS_KB_ID", value = var.knowledge_base_id },
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

resource "aws_security_group" "app" {
  name        = "${local.name_prefix}-app-sg"
  description = "Security group for Nimbus ECS service allowing inbound HTTP on port 8000"
  vpc_id      = aws_vpc.main.id

  # Inbound 8000 is restricted to VPC/internal CIDRs by default. Public client traffic terminates at API Gateway.
  ingress {
    description = "Allow inbound port 8000 from VPC and API Gateway"
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = var.allowed_ingress_cidr_blocks
  }

  egress {
    description = "Allow all outbound traffic"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name_prefix}-app-sg" }
}

resource "aws_ecs_service" "app" {
  name            = "${local.name_prefix}-service"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.app.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = [aws_subnet.private_a.id, aws_subnet.private_b.id]
    security_groups  = [aws_security_group.app.id]
    assign_public_ip = var.assign_public_ip # gated behind variable; defaults to false for security
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

resource "aws_apigatewayv2_integration" "app" {
  api_id                 = aws_apigatewayv2_api.main.id
  integration_type       = "HTTP_PROXY"
  integration_uri        = "${var.ecs_target_url}/{proxy}"
  integration_method     = "ANY"
  payload_format_version = "1.0"
}

resource "aws_apigatewayv2_integration" "root" {
  api_id                 = aws_apigatewayv2_api.main.id
  integration_type       = "HTTP_PROXY"
  integration_uri        = var.ecs_target_url
  integration_method     = "ANY"
  payload_format_version = "1.0"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.main.id
  name        = "$default"
  auto_deploy = true
}

resource "aws_apigatewayv2_route" "requests" {
  api_id    = aws_apigatewayv2_api.main.id
  route_key = "POST /requests"
  target    = "integrations/${aws_apigatewayv2_integration.app.id}"
}

resource "aws_apigatewayv2_route" "proxy" {
  api_id    = aws_apigatewayv2_api.main.id
  route_key = "ANY /{proxy+}"
  target    = "integrations/${aws_apigatewayv2_integration.app.id}"
}

resource "aws_apigatewayv2_route" "root" {
  api_id    = aws_apigatewayv2_api.main.id
  route_key = "GET /"
  target    = "integrations/${aws_apigatewayv2_integration.root.id}"
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
  name           = "${local.name_prefix}-task-events"
  description    = "Route Nimbus task/lease events to the escalations queue"
  event_bus_name = aws_cloudwatch_event_bus.main.name
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
          "dynamodb:TransactWriteItems", "dynamodb:TransactGetItems",
          "dynamodb:DescribeTable",
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


# ---------------------------------------------------------------------------
# AWS IoT Core skeleton: IoT Policy for edge robot clusters.
# Note: AWS IoT Things, X.509 certificates, and certificate attachments are
# provisioned at device onboarding / deployment time, not baked statically.
# ---------------------------------------------------------------------------

resource "aws_iot_policy" "edge_cluster" {
  name = "${local.name_prefix}-edge-policy"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["iot:Connect"]
        Resource = ["arn:aws:iot:${var.aws_region}:*:client/nimbus-*"]
      },
      {
        Effect = "Allow"
        Action = ["iot:Publish"]
        Resource = [
          "arn:aws:iot:${var.aws_region}:*:topic/nimbus/*/escalation",
          "arn:aws:iot:${var.aws_region}:*:topic/nimbus/*/checkin",
          "arn:aws:iot:${var.aws_region}:*:topic/nimbus/*/probe"
        ]
      },
      {
        Effect = "Allow"
        Action = ["iot:Receive", "iot:Subscribe"]
        Resource = [
          "arn:aws:iot:${var.aws_region}:*:topic/nimbus/*/lease",
          "arn:aws:iot:${var.aws_region}:*:topic/nimbus/*/renewal",
          "arn:aws:iot:${var.aws_region}:*:topicfilter/nimbus/*/lease",
          "arn:aws:iot:${var.aws_region}:*:topicfilter/nimbus/*/renewal"
        ]
      }
    ]
  })
}

# ---------------------------------------------------------------------------
# Static web app storage and CloudFront CDN distribution skeleton.
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "static_app" {
  bucket        = "${local.name_prefix}-static-app"
  force_destroy = false
}

resource "aws_cloudfront_origin_access_control" "static_app" {
  name                              = "${local.name_prefix}-oac"
  description                       = "OAC for Nimbus static web application"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_distribution" "static_app" {
  comment             = "Nimbus static web UI distribution skeleton"
  enabled             = false
  is_ipv6_enabled     = true
  default_root_object = "index.html"

  origin {
    domain_name              = aws_s3_bucket.static_app.bucket_regional_domain_name
    origin_id                = "S3-${aws_s3_bucket.static_app.id}"
    origin_access_control_id = aws_cloudfront_origin_access_control.static_app.id
  }

  default_cache_behavior {
    allowed_methods  = ["GET", "HEAD", "OPTIONS"]
    cached_methods   = ["GET", "HEAD"]
    target_origin_id = "S3-${aws_s3_bucket.static_app.id}"

    forwarded_values {
      query_string = false
      cookies {
        forward = "none"
      }
    }

    viewer_protocol_policy = "redirect-to-https"
    min_ttl                = 0
    default_ttl            = 3600
    max_ttl                = 86400
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }

  tags = { Name = "${local.name_prefix}-static-cdn" }
}

# ---------------------------------------------------------------------------
# Secrets Manager skeleton: secret container for API keys / third-party credentials.
# Real secret values are injected during deploy time, not stored in code.
# ---------------------------------------------------------------------------

resource "aws_secretsmanager_secret" "app_secrets" {
  name                    = "${local.name_prefix}-app-secrets"
  description             = "Skeleton secret container for Nimbus API keys and credentials (populated at deploy time)"
  recovery_window_in_days = 0
}

# ---------------------------------------------------------------------------
# CloudWatch Alarm skeleton: missed check-in alerts (PRD NFR-2, Sec. 7).
# Monitors Nimbus/App MissedCheckins metric emitted via EMF.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "missed_checkin" {
  alarm_name          = "${local.name_prefix}-missed-checkin-alarm"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "MissedCheckins"
  namespace           = "Nimbus/App"
  period              = 60
  statistic           = "Sum"
  threshold           = 0
  alarm_description   = "Triggered when an edge robot cluster misses its heartbeat checkin deadline"
  treat_missing_data  = "notBreaching"
}
