# IAM and secrets audit (NFR-5)

- IAM statements with a wildcard: 2 (0 unjustified)
- Secrets found in tracked source: 0

| file | statement | problem | justification |
|---|---|---|---|
| p3-dev-policy.json | BedrockInvokeAndDiscover | wildcard resource | Bedrock invocation resources are foundation-model and inference-profile ARNs that differ per region |
| p3-dev-policy.json | DynamoDbListTables | wildcard resource | dynamodb:ListTables does not support resource-level permissions in IAM |

**RESULT: PASS**
