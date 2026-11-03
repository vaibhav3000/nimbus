#!/usr/bin/env bash
# Build the Nimbus service image and push it to the ECR repository Terraform created (P3 owns the image).
#
#   AWS_PROFILE=nimbus-p3 AWS_REGION=ap-south-1 scripts/build_push.sh [tag]      # default tag: latest
#
# Needs: Docker running, AWS credentials, and the repository to exist
# (`terraform apply -target=aws_ecr_repository.app` in infra/terraform, run by P4).
# The image is built for linux/amd64 because the Fargate task definition does not ask for ARM; building on an
# Apple Silicon Mac without --platform would give an image that fails on Fargate with "exec format error".
set -euo pipefail
cd "$(dirname "$0")/.."
TAG="${1:-latest}"
REGION="${AWS_REGION:-ap-south-1}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
REPO="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/nimbus-app"

aws ecr describe-repositories --repository-names nimbus-app --region "$REGION" >/dev/null \
  || { echo "ECR repository nimbus-app not found: ask P4 to run 'terraform apply -target=aws_ecr_repository.app'" >&2; exit 1; }

aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"
docker build --platform linux/amd64 -t "nimbus-app:${TAG}" .
docker tag "nimbus-app:${TAG}" "${REPO}:${TAG}"
docker push "${REPO}:${TAG}"
echo "pushed ${REPO}:${TAG}"
echo "Terraform uses ${REPO}:latest by default (var.app_image is empty); after pushing, P4 applies and the service pulls it."
