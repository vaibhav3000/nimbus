# Person 3 additions to P4's skeleton: lease/claim state table, permissions the
# cloud decision plane needs, internet egress for the Fargate task, and a
# budget alarm. SKELETON ONLY: validate, never apply without the account owner.

data "aws_caller_identity" "current" {}

# ---------------------------------------------------------------------------
# State table: lease task pointers, robot/zone claims, message-id dedupe,
# task-id counter. TTL on `ttl` expires claims and dedupe ids on their own.
# Item layout is documented in cloud/aws_dynamo.py.
# ---------------------------------------------------------------------------

resource "aws_dynamodb_table" "state" {
  name         = "${local.name_prefix}-state"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"

  attribute {
    name = "pk"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }
}

# ---------------------------------------------------------------------------
# Egress. The skeleton's private subnets had no route out, so the Fargate task
# could neither pull its image nor reach Bedrock. "public_ip" (default) is the
# cheapest working option: an internet gateway and a public IP per task, with
# nothing listening (no inbound security-group rules). A NAT gateway costs
# roughly $32+/month, so it is deliberately not the default.
# ---------------------------------------------------------------------------

resource "aws_internet_gateway" "main" {
  count  = var.egress_mode == "public_ip" ? 1 : 0
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${local.name_prefix}-igw" }
}

resource "aws_route_table" "egress" {
  count  = var.egress_mode == "public_ip" ? 1 : 0
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main[0].id
  }
}

resource "aws_route_table_association" "egress_a" {
  count          = var.egress_mode == "public_ip" ? 1 : 0
  subnet_id      = aws_subnet.private_a.id
  route_table_id = aws_route_table.egress[0].id
}

resource "aws_route_table_association" "egress_b" {
  count          = var.egress_mode == "public_ip" ? 1 : 0
  subnet_id      = aws_subnet.private_b.id
  route_table_id = aws_route_table.egress[0].id
}

# ---------------------------------------------------------------------------
# Permissions the decision plane needs beyond P4's baseline policy.
# - state table (conditional writes and transactions use Put/Update/Delete)
# - Scan on tasks (startup recovery of queued work)
# - Bedrock: Converse needs bedrock:InvokeModel. Cross-region inference
#   profiles route to other regions, so the foundation-model ARN is region-wild
#   and the profile ARN is account-scoped.
# - IoT topics exactly as contracts/topics.md names them (P4's iot_topics
#   locals use a different pattern, which would deny these publishes).
# ---------------------------------------------------------------------------

resource "aws_iam_role_policy" "task_p3" {
  name = "${local.name_prefix}-task-policy-p3"
  role = aws_iam_role.task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "StateTable"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
          "dynamodb:DeleteItem", "dynamodb:Query", "dynamodb:Scan",
        ]
        Resource = [aws_dynamodb_table.state.arn]
      },
      {
        Sid      = "TasksScan"
        Effect   = "Allow"
        Action   = ["dynamodb:Scan"]
        Resource = [aws_dynamodb_table.tasks.arn]
      },
      {
        Sid    = "BedrockConverse"
        Effect = "Allow"
        Action = ["bedrock:InvokeModel"]
        Resource = [
          "arn:aws:bedrock:*::foundation-model/*",
          "arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:inference-profile/*",
        ]
      },
      {
        Sid    = "IotPublish"
        Effect = "Allow"
        Action = ["iot:Publish"]
        Resource = [
          for t in ["lease", "renewal", "probe"] :
          "arn:aws:iot:${var.aws_region}:${data.aws_caller_identity.current.account_id}:topic/nimbus/*/${t}"
        ]
      },
      {
        Sid    = "IotSubscribe"
        Effect = "Allow"
        Action = ["iot:Subscribe", "iot:Receive"]
        Resource = [
          for t in ["escalation", "checkin"] :
          "arn:aws:iot:${var.aws_region}:${data.aws_caller_identity.current.account_id}:topic/nimbus/*/${t}"
        ]
      },
    ]
  })
}

# ---------------------------------------------------------------------------
# Cost guard (PRD risk R3: budget alarms from week 1). Created only when an
# alert address is given.
# ---------------------------------------------------------------------------

resource "aws_budgets_budget" "monthly" {
  count        = var.alert_email == "" ? 0 : 1
  name         = "${local.name_prefix}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  dynamic "notification" {
    for_each = [50, 80, 100]
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "PERCENTAGE"
      notification_type          = "ACTUAL"
      subscriber_email_addresses = [var.alert_email]
    }
  }
}
