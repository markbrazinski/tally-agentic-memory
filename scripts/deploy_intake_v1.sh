#!/usr/bin/env bash
# Build and deploy a separate Intake v1 App Runner service without changing judged routing.
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
PROFILE="${AWS_PROFILE:-gate5-deployer}"
ACCOUNT_ID="$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text)"
SERVICE_NAME="tally-intake-v1"
REPOSITORY="tally-intake-v1"
RUNTIME_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/tally-intake-v1-runtime"
ACCESS_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/tally-intake-v1-ecr-access"
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${REPOSITORY}"
IMAGE_TAG="$(git rev-parse --short=12 HEAD)"
PREFIX="/tally/intake-v1"

if [ -n "$(git status --porcelain)" ]; then
  echo "STOP: isolated deployment requires the exact clean committed source" >&2
  exit 1
fi

# The SPA carries the demo bearer token (no per-user login on this lane), baked
# into the bundle at build time. Read it from SSM and pass as a build arg.
DEMO_TOKEN="$(aws ssm get-parameter --name "${PREFIX}/demo-token" --with-decryption \
  --profile "$PROFILE" --region "$REGION" --query 'Parameter.Value' --output text)"
docker build --platform linux/amd64 \
  --build-arg "VITE_DEMO_TOKEN=${DEMO_TOKEN}" \
  -t "${REPOSITORY}:${IMAGE_TAG}" -f Dockerfile .
aws ecr get-login-password --profile "$PROFILE" --region "$REGION" |
  docker login --username AWS --password-stdin \
    "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com" >/dev/null
docker tag "${REPOSITORY}:${IMAGE_TAG}" "${ECR_URI}:${IMAGE_TAG}"
docker push "${ECR_URI}:${IMAGE_TAG}" >/dev/null

SOURCE_CONFIG="$(jq -n \
  --arg image "${ECR_URI}:${IMAGE_TAG}" --arg access "$ACCESS_ROLE_ARN" \
  --arg region "$REGION" --arg account "$ACCOUNT_ID" --arg prefix "$PREFIX" '{
  ImageRepository:{
    ImageIdentifier:$image,
    ImageRepositoryType:"ECR",
    ImageConfiguration:{
      Port:"8000",
      RuntimeEnvironmentSecrets:{
        TALLY_CRDB_DSN:("arn:aws:ssm:"+$region+":"+$account+":parameter"+$prefix+"/crdb-dsn"),
        TALLY_TENANT_ID:("arn:aws:ssm:"+$region+":"+$account+":parameter"+$prefix+"/tenant-id"),
        TALLY_DEMO_TOKEN:("arn:aws:ssm:"+$region+":"+$account+":parameter"+$prefix+"/demo-token")
      },
      RuntimeEnvironmentVariables:{
        AWS_REGION:$region,
        TALLY_INTAKE_BUCKET:"tally-record",
        TALLY_INTAKE_KEY_PREFIX:"isolated-intake-v1",
        TALLY_INTAKE_DEMO_ENABLED:"true",
        TALLY_INTAKE_WORKER_ENABLED:"true",
        TALLY_PUBLIC_DEMO_ENABLED:"false",
        TALLY_STATIC_DIR:"/app/ui",
        # Managed MCP OAuth (reconstruction worker reads these at runtime — the
        # bundle SSM param the canary refreshes + the refresh lease table). Must
        # be in the deploy block or a redeploy regresses reconstruction to
        # KeyError: 'TALLY_OAUTH_TOKEN_PARAMETER'.
        TALLY_OAUTH_TOKEN_PARAMETER:"/tally/gate5/oauth-token-bundle",
        TALLY_OAUTH_REFRESH_LEASE_TABLE:"tally-gate5-oauth-refresh-lease",
        # Managed MCP scope (reconstruction reads these too; absent → the worker
        # dies with MCPPermissionError "missing ... cluster_id, database", which
        # surfaces confusingly as MCP_UNAUTHORIZED). service_identity +
        # permission_mode are hardcoded for the OAuth runtime path, so only the
        # cluster + database need to be in env.
        TALLY_MCP_CLUSTER_ID:"51445757-7351-4f55-b545-1bc84a4f6a55",
        TALLY_MCP_DATABASE:"tally_intake_deployed_20260723"
      }
    }
  },
  AutoDeploymentsEnabled:false,
  AuthenticationConfiguration:{AccessRoleArn:$access}
}')"

SERVICE_ARN="$(aws apprunner list-services --profile "$PROFILE" --region "$REGION" \
  --query "ServiceSummaryList[?ServiceName=='${SERVICE_NAME}'].ServiceArn | [0]" \
  --output text)"
PREVIOUS_UPDATED_AT=""
if [ "$SERVICE_ARN" = "None" ] || [ -z "$SERVICE_ARN" ]; then
  SERVICE_ARN="$(aws apprunner create-service --profile "$PROFILE" --region "$REGION" \
    --service-name "$SERVICE_NAME" \
    --source-configuration "$SOURCE_CONFIG" \
    --instance-configuration \
      "Cpu=0.25 vCPU,Memory=0.5 GB,InstanceRoleArn=${RUNTIME_ROLE_ARN}" \
    --health-check-configuration \
      'Protocol=HTTP,Path=/readyz,Interval=10,Timeout=5,HealthyThreshold=1,UnhealthyThreshold=3' \
    --network-configuration \
      'IngressConfiguration={IsPubliclyAccessible=true}' \
      --tags Key=Project,Value=Tally Key=Environment,Value=intake-v1-isolated \
      --query Service.ServiceArn --output text)"
else
  PREVIOUS_UPDATED_AT="$(aws apprunner describe-service \
    --profile "$PROFILE" --region "$REGION" --service-arn "$SERVICE_ARN" \
    --query Service.UpdatedAt --output text)"
  aws apprunner update-service --profile "$PROFILE" --region "$REGION" \
    --service-arn "$SERVICE_ARN" \
    --source-configuration "$SOURCE_CONFIG" \
    --instance-configuration \
      "Cpu=0.25 vCPU,Memory=0.5 GB,InstanceRoleArn=${RUNTIME_ROLE_ARN}" >/dev/null
fi

for _ in $(seq 1 60); do
  SERVICE_STATE="$(aws apprunner describe-service --profile "$PROFILE" \
    --region "$REGION" --service-arn "$SERVICE_ARN" \
    --query 'Service.[Status,UpdatedAt]' --output text)"
  STATUS="${SERVICE_STATE%%[[:space:]]*}"
  UPDATED_AT="${SERVICE_STATE##*[[:space:]]}"
  if [ "$STATUS" = "RUNNING" ] && {
    [ -z "$PREVIOUS_UPDATED_AT" ] || [ "$UPDATED_AT" != "$PREVIOUS_UPDATED_AT" ]
  }; then
    break
  fi
  case "$STATUS" in
    CREATE_FAILED|UPDATE_FAILED|DELETE_FAILED)
      echo "STOP: isolated App Runner service failed" >&2
      exit 1
      ;;
  esac
  sleep 10
done
[ "${STATUS:-}" = "RUNNING" ] || {
  echo "STOP: isolated App Runner service did not become ready" >&2
  exit 1
}

SERVICE_URL="$(aws apprunner describe-service --profile "$PROFILE" --region "$REGION" \
  --service-arn "$SERVICE_ARN" --query Service.ServiceUrl --output text)"
curl --fail --silent --show-error --max-time 20 \
  "https://${SERVICE_URL}/readyz" >/dev/null
echo "https://${SERVICE_URL}"
