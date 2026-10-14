# Nimbus cloud service: the API, the requester/operator apps and the P3 decision plane.
# Built once, pushed to ECR, run by the Fargate service in infra/terraform (var.app_image).
#
#   docker build -t nimbus-app .
#   docker run -p 8000:8000 -e NIMBUS_BACKEND=local -e NIMBUS_LLM=fake nimbus-app   # no AWS needed
#
# In AWS the task's IAM role supplies credentials; no keys are baked in or passed.
# Base image comes from the ECR Public mirror of Docker Official Images (no Docker Hub pull limits).
FROM public.ecr.aws/docker/library/python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

# Runtime dependencies only (pytest and moto are test-only).
RUN pip install --no-cache-dir "jsonschema>=4.21" "boto3>=1.34"

COPY server.py contracts.py cost.py store.py ./
COPY contracts/ contracts/
COPY cloud/ cloud/
COPY packs/ packs/
COPY static/ static/

# Defaults for the deployed service; Terraform's container definition can override any of them.
ENV NIMBUS_BACKEND=aws \
    NIMBUS_LLM=bedrock \
    NIMBUS_BEDROCK_MODEL=apac.amazon.nova-lite-v1:0 \
    NIMBUS_DDB_PREFIX=nimbus-ds252 \
    NIMBUS_MODE=C1 \
    AWS_REGION=ap-south-1 \
    PORT=8000 \
    DATA_DIR=/app/data

RUN useradd --create-home nimbus && mkdir -p /app/data && chown nimbus /app/data
USER nimbus

EXPOSE 8000
CMD ["python", "server.py"]
