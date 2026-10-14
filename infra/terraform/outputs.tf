output "api_endpoint" {
  description = "HTTP API endpoint the requester app and operator console call."
  value       = aws_apigatewayv2_api.main.api_endpoint
}

output "escalations_queue_url" {
  description = "SQS queue receiving escalations and check-ins (Person 3's ingress)."
  value       = aws_sqs_queue.escalations.url
}

output "outputs_bucket" {
  description = "S3 bucket for task outputs (result_ref targets)."
  value       = aws_s3_bucket.outputs.bucket
}

output "table_names" {
  description = "DynamoDB tables replacing the local file store."
  value = {
    leases       = aws_dynamodb_table.leases.name
    tasks        = aws_dynamodb_table.tasks.name
    robot_status = aws_dynamodb_table.robot_status.name
    decision_log = aws_dynamodb_table.decision_log.name
  }
}

output "iot_topics" {
  description = "Cloud-edge messaging topic names (leases down, escalations/check-ins up)."
  value       = local.iot_topics
}

output "state_table" {
  description = "Lease/claim/dedupe state table (cloud/aws_dynamo.py)."
  value       = aws_dynamodb_table.state.name
}
