#!/usr/bin/env bash
# Deploy the Tally FastAPI app to AWS App Runner. Uses the scoped `tally`
# AWS CLI profile - never default/root credentials (see CLAUDE.md AWS
# account section).
#
# Prerequisites (not created by this script):
#   - App Runner access role (`example-service-runner-access-role`, trust:
#     build.apprunner.amazonaws.com) with ecr:GetDownloadUrlForLayer/
#     BatchGetImage/GetAuthorizationToken on the example-recovery-service ECR repo.
#   - App Runner instance role (`example-service-runner-instance-role`, trust:
#     tasks.apprunner.amazonaws.com) with ssm:GetParameter on
#     /example/tally/crdb-dsn and /example/tally/demo-token, and the Bedrock InvokeModel
#     permissions already granted to example-archive-worker-scoped-policy
#     (App Runner's instance role, not the deploying user, is what calls
#     Bedrock at runtime - it needs its own copy of that grant).
#   - SSM parameters /example/tally/crdb-dsn and /example/tally/demo-token already set
#     (crdb_dsn was set in Bundle R; demo_token set once this session via
#     `aws ssm put-parameter --name /example/tally/demo-token ...`).
#
# Every step ends in a read-back assertion (CLAUDE.md standing lock: "a
# deploy isn't done when the API call succeeds - it's done when the
# script reads back live state and asserts it matches intent"), same
# discipline as deploy.sh's Target.Input check.
set -euo pipefail

SERVICE_NAME="example-recovery-service"
REGION="${AWS_REGION:-us-east-1}"
PROFILE="${AWS_PROFILE:-example-profile}"
ACCOUNT_ID="000000000000"
ECR_REPO_NAME="example-recovery-service"
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${ECR_REPO_NAME}"
ACCESS_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/example-service-runner-access-role"
INSTANCE_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/example-service-runner-instance-role"
IMAGE_TAG="$(git rev-parse --short HEAD 2>/dev/null || echo "manual")"

# Not a secret - a plain RuntimeEnvironmentVariable, not SSM. Vite dev
# origin baked in as the floor; TALLY_STATIC_ORIGIN (unset today) is the
# FE's static-host placeholder, folded in automatically once they set it
# via TALLY_STATIC_ORIGIN below without touching this script.
CORS_ORIGINS="${TALLY_CORS_ORIGINS:-http://localhost:5173}"
STATIC_ORIGIN="${TALLY_STATIC_ORIGIN:-}"

echo "== Building Docker image =="
# App Runner only runs x86_64 images; force linux/amd64 so an arm64 build
# host (Apple Silicon) doesn't produce an image that fails to exec on the
# x86 App Runner host - the "health check on port 8000 fails, container
# logs never appear" failure mode.
docker build --platform linux/amd64 -t "${ECR_REPO_NAME}:${IMAGE_TAG}" -f Dockerfile .

echo "== Ensuring ECR repository exists =="
if ! aws ecr describe-repositories --profile "$PROFILE" --region "$REGION" \
    --repository-names "$ECR_REPO_NAME" >/dev/null 2>&1; then
  aws ecr create-repository --profile "$PROFILE" --region "$REGION" \
    --repository-name "$ECR_REPO_NAME"
fi

echo "== Pushing image to ECR =="
aws ecr get-login-password --profile "$PROFILE" --region "$REGION" \
  | docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
docker tag "${ECR_REPO_NAME}:${IMAGE_TAG}" "${ECR_URI}:${IMAGE_TAG}"
docker tag "${ECR_REPO_NAME}:${IMAGE_TAG}" "${ECR_URI}:latest"
docker push "${ECR_URI}:${IMAGE_TAG}"
docker push "${ECR_URI}:latest"

# Fail fast if the image is the wrong CPU type. App Runner only runs
# amd64; an arm64 image (default on Apple Silicon) fails ~26 min later
# with an unhelpful "health check on port 8000" error. Catch it in 2s.
echo "== Verifying image architecture is amd64 =="
IMG_ARCH=$(docker image inspect "${ECR_REPO_NAME}:${IMAGE_TAG}" --format '{{.Architecture}}')
if [ "$IMG_ARCH" != "amd64" ]; then
  echo "STOP: image is '$IMG_ARCH', App Runner needs 'amd64'. The build's --platform flag is missing or ignored - do not deploy." >&2
  exit 1
fi
echo "  arch: $IMG_ARCH (ok)"

echo "== Creating/updating App Runner service =="
SOURCE_CONFIG=$(cat <<JSON
{
  "ImageRepository": {
    "ImageIdentifier": "${ECR_URI}:${IMAGE_TAG}",
    "ImageRepositoryType": "ECR",
    "ImageConfiguration": {
      "Port": "8000",
      "RuntimeEnvironmentSecrets": {
        "TALLY_CRDB_DSN": "arn:aws:ssm:${REGION}:${ACCOUNT_ID}:parameter/example/tally/crdb-dsn",
        "TALLY_DEMO_TOKEN": "arn:aws:ssm:${REGION}:${ACCOUNT_ID}:parameter/example/tally/demo-token"
      },
      "RuntimeEnvironmentVariables": {
        "TALLY_CORS_ORIGINS": "${CORS_ORIGINS}",
        "TALLY_STATIC_ORIGIN": "${STATIC_ORIGIN}"
      }
    }
  },
  "AutoDeploymentsEnabled": false,
  "AuthenticationConfiguration": {
    "AccessRoleArn": "${ACCESS_ROLE_ARN}"
  }
}
JSON
)

if aws apprunner describe-service --profile "$PROFILE" --region "$REGION" \
    --service-arn "arn:aws:apprunner:${REGION}:${ACCOUNT_ID}:service/${SERVICE_NAME}" >/dev/null 2>&1; then
  SERVICE_ARN="arn:aws:apprunner:${REGION}:${ACCOUNT_ID}:service/${SERVICE_NAME}"
  aws apprunner update-service --profile "$PROFILE" --region "$REGION" \
    --service-arn "$SERVICE_ARN" \
    --source-configuration "$SOURCE_CONFIG" \
    --instance-configuration "InstanceRoleArn=${INSTANCE_ROLE_ARN}"
else
  CREATE_OUTPUT=$(aws apprunner create-service --profile "$PROFILE" --region "$REGION" \
    --service-name "$SERVICE_NAME" \
    --source-configuration "$SOURCE_CONFIG" \
    --instance-configuration "InstanceRoleArn=${INSTANCE_ROLE_ARN}")
  SERVICE_ARN=$(echo "$CREATE_OUTPUT" | python3 -c "import json,sys; print(json.load(sys.stdin)['Service']['ServiceArn'])")
fi

echo "== Waiting for service to reach RUNNING =="
for i in $(seq 1 60); do
  STATUS=$(aws apprunner describe-service --profile "$PROFILE" --region "$REGION" \
    --service-arn "$SERVICE_ARN" --query "Service.Status" --output text)
  echo "  status: $STATUS (attempt $i/60)"
  if [ "$STATUS" = "RUNNING" ]; then
    break
  fi
  if [ "$STATUS" = "CREATE_FAILED" ] || [ "$STATUS" = "OPERATION_IN_PROGRESS" ] && [ "$i" -eq 60 ]; then
    echo "DEPLOY FAILED: service status is $STATUS after 60 checks" >&2
    exit 1
  fi
  sleep 15
done

echo "== Verifying live service state matches intent (read-back assertion) =="
LIVE_STATUS=$(aws apprunner describe-service --profile "$PROFILE" --region "$REGION" \
  --service-arn "$SERVICE_ARN" --query "Service.Status" --output text)
if [ "$LIVE_STATUS" != "RUNNING" ]; then
  echo "DEPLOY FAILED: live service status is '$LIVE_STATUS', expected 'RUNNING'" >&2
  exit 1
fi

LIVE_IMAGE=$(aws apprunner describe-service --profile "$PROFILE" --region "$REGION" \
  --service-arn "$SERVICE_ARN" \
  --query "Service.SourceConfiguration.ImageRepository.ImageIdentifier" --output text)
if [ "$LIVE_IMAGE" != "${ECR_URI}:${IMAGE_TAG}" ]; then
  echo "DEPLOY FAILED: live image is '$LIVE_IMAGE', expected '${ECR_URI}:${IMAGE_TAG}'" >&2
  exit 1
fi

# Same class of bug as the EventBridge Target.Input incident (CLAUDE.md
# standing lock): an env var that didn't actually take live would produce
# exactly "middleware added but no ACAO headers" - caught here, not left
# for the functional curl gate to discover late.
LIVE_CORS_ORIGINS=$(aws apprunner describe-service --profile "$PROFILE" --region "$REGION" \
  --service-arn "$SERVICE_ARN" \
  --query "Service.SourceConfiguration.ImageRepository.ImageConfiguration.RuntimeEnvironmentVariables.TALLY_CORS_ORIGINS" \
  --output text)
if [ "$LIVE_CORS_ORIGINS" != "$CORS_ORIGINS" ]; then
  echo "DEPLOY FAILED: live TALLY_CORS_ORIGINS is '$LIVE_CORS_ORIGINS', expected '$CORS_ORIGINS'" >&2
  exit 1
fi

SERVICE_URL=$(aws apprunner describe-service --profile "$PROFILE" --region "$REGION" \
  --service-arn "$SERVICE_ARN" --query "Service.ServiceUrl" --output text)

echo "== Verifying the deployed URL actually responds =="
HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" "https://${SERVICE_URL}/healthz")
if [ "$HTTP_STATUS" != "200" ]; then
  echo "DEPLOY FAILED: GET https://${SERVICE_URL}/healthz returned $HTTP_STATUS, expected 200" >&2
  exit 1
fi

echo "== Done =="
echo "  Service ARN: $SERVICE_ARN"
echo "  Live URL: https://${SERVICE_URL}"
echo "  Image: ${ECR_URI}:${IMAGE_TAG}"
