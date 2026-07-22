#!/usr/bin/env bash
# Build and deploy the combined Tally UI/API judge service to AWS App Runner.
# The script accepts no secret values; App Runner resolves only SSM parameter
# ARNs into its server-side environment.
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
PROFILE="${AWS_PROFILE:?set AWS_PROFILE to the scoped Gate 5 deployer profile}"
SERVICE_NAME="${TALLY_GATE5_SERVICE_NAME:-tally-gate5-demo}"
ECR_REPO_NAME="${TALLY_GATE5_ECR_REPOSITORY:-tally-gate5-demo}"
ACCESS_ROLE_NAME="${TALLY_GATE5_ACCESS_ROLE_NAME:-tally-gate5-apprunner-ecr}"
INSTANCE_ROLE_NAME="${TALLY_GATE5_INSTANCE_ROLE_NAME:-tally-gate5-apprunner-runtime}"
PARAM_PREFIX="/tally/gate5"
OAUTH_PARAMETER="${TALLY_OAUTH_TOKEN_PARAMETER:-${PARAM_PREFIX}/oauth-token-bundle}"
LEASE_TABLE_NAME="${TALLY_OAUTH_REFRESH_LEASE_TABLE:-tally-gate5-oauth-refresh-lease}"
CANDIDATE_MODE="${TALLY_GATE5_CANDIDATE_MODE:-false}"
PYTHON="${TALLY_PYTHON:-python3}"
IMAGE_TAG="$(git rev-parse --short=12 HEAD)"

if [ -n "$(git status --porcelain)" ]; then
  if [ "$CANDIDATE_MODE" != "true" ]; then
    echo "STOP: dirty deployment requires explicit pre-verdict candidate mode" >&2
    exit 1
  fi
  IMAGE_TAG="candidate-pending"
fi

: "${TALLY_CCLOUD_CLUSTER_ID:?set the private expected cluster ID for preflight}"
"$PYTHON" -m scripts.gate5_ccloud_preflight >/dev/null
"$PYTHON" -m scripts.gate5b_deployment_preflight >/dev/null
"$PYTHON" -m scripts.gate5b_token_leak_scan >/dev/null
# First invocation creates and verifies the budget before App Runner exists.
scripts/gate5_guardrails.sh >/dev/null

ACCOUNT_ID=$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text)
if [[ ! "$ACCOUNT_ID" =~ ^[0-9]{12}$ ]]; then
  echo "STOP: scoped AWS identity did not return a valid account" >&2
  exit 1
fi
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${ECR_REPO_NAME}"
ACCESS_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ACCESS_ROLE_NAME}"
INSTANCE_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${INSTANCE_ROLE_NAME}"
AUTOSCALING_NAME="tally-gate5-single-instance"

AUTOSCALING_ARN=$(aws apprunner list-auto-scaling-configurations --profile "$PROFILE" \
  --region "$REGION" --auto-scaling-configuration-name "$AUTOSCALING_NAME" \
  --latest-only --query 'AutoScalingConfigurationSummaryList[0].AutoScalingConfigurationArn' \
  --output text)
if [ "$AUTOSCALING_ARN" = "None" ] || [ -z "$AUTOSCALING_ARN" ]; then
  AUTOSCALING_ARN=$(aws apprunner create-auto-scaling-configuration --profile "$PROFILE" \
    --region "$REGION" --auto-scaling-configuration-name "$AUTOSCALING_NAME" \
    --max-concurrency 20 --min-size 1 --max-size 1 \
    --tags Key=Project,Value=Tally Key=Environment,Value=synthetic-gate5 \
      Key=TeardownDate,Value=2026-09-30 \
    --query 'AutoScalingConfiguration.AutoScalingConfigurationArn' --output text)
fi
AUTOSCALING_LIVE=$(aws apprunner describe-auto-scaling-configuration --profile "$PROFILE" \
  --region "$REGION" --auto-scaling-configuration-arn "$AUTOSCALING_ARN" --output json)
if [ "$(jq -r '.AutoScalingConfiguration.MaxConcurrency' <<<"$AUTOSCALING_LIVE")" != "20" ] \
  || [ "$(jq -r '.AutoScalingConfiguration.MinSize' <<<"$AUTOSCALING_LIVE")" != "1" ] \
  || [ "$(jq -r '.AutoScalingConfiguration.MaxSize' <<<"$AUTOSCALING_LIVE")" != "1" ]; then
  AUTOSCALING_ARN=$(aws apprunner create-auto-scaling-configuration --profile "$PROFILE" \
    --region "$REGION" --auto-scaling-configuration-name "$AUTOSCALING_NAME" \
    --max-concurrency 20 --min-size 1 --max-size 1 \
    --tags Key=Project,Value=Tally Key=Environment,Value=synthetic-gate5 \
      Key=TeardownDate,Value=2026-09-30 \
    --query 'AutoScalingConfiguration.AutoScalingConfigurationArn' --output text)
fi

docker build --platform linux/amd64 -t "${ECR_REPO_NAME}:${IMAGE_TAG}" -f Dockerfile .
if [ "$(docker image inspect "${ECR_REPO_NAME}:${IMAGE_TAG}" --format '{{.Architecture}}')" != "amd64" ]; then
  echo "STOP: App Runner image must be amd64" >&2
  exit 1
fi
if [ "$CANDIDATE_MODE" = "true" ]; then
  IMAGE_ID=$(docker image inspect "${ECR_REPO_NAME}:${IMAGE_TAG}" --format '{{.Id}}')
  if [[ ! "$IMAGE_ID" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    echo "STOP: candidate image did not have a content-addressed image ID" >&2
    exit 1
  fi
  CONTENT_TAG="candidate-${IMAGE_ID:7:12}"
  docker tag "${ECR_REPO_NAME}:${IMAGE_TAG}" "${ECR_REPO_NAME}:${CONTENT_TAG}"
  IMAGE_TAG="$CONTENT_TAG"
fi

aws ecr get-login-password --profile "$PROFILE" --region "$REGION" \
  | docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com" >/dev/null
docker tag "${ECR_REPO_NAME}:${IMAGE_TAG}" "${ECR_URI}:${IMAGE_TAG}"
docker push "${ECR_URI}:${IMAGE_TAG}" >/dev/null

SOURCE_CONFIG=$(jq -n \
  --arg image "${ECR_URI}:${IMAGE_TAG}" \
  --arg access "$ACCESS_ROLE_ARN" \
  --arg region "$REGION" \
  --arg account "$ACCOUNT_ID" \
  --arg prefix "$PARAM_PREFIX" \
  --arg oauth "$OAUTH_PARAMETER" \
  --arg lease "$LEASE_TABLE_NAME" \
  '{
    ImageRepository:{
      ImageIdentifier:$image,
      ImageRepositoryType:"ECR",
      ImageConfiguration:{
        Port:"8000",
        RuntimeEnvironmentSecrets:{
          TALLY_CRDB_DSN:("arn:aws:ssm:"+$region+":"+$account+":parameter"+$prefix+"/crdb-dsn"),
          TALLY_TENANT_ID:("arn:aws:ssm:"+$region+":"+$account+":parameter"+$prefix+"/tenant-id"),
          TALLY_PUBLIC_DEMO_CASE_ID:("arn:aws:ssm:"+$region+":"+$account+":parameter"+$prefix+"/hero-case-id"),
          TALLY_PUBLIC_DEMO_CONTEST_ID:("arn:aws:ssm:"+$region+":"+$account+":parameter"+$prefix+"/hero-contest-id"),
          TALLY_MCP_CLUSTER_ID:("arn:aws:ssm:"+$region+":"+$account+":parameter"+$prefix+"/mcp-cluster-id"),
          TALLY_MCP_DATABASE:("arn:aws:ssm:"+$region+":"+$account+":parameter"+$prefix+"/mcp-database")
        },
        RuntimeEnvironmentVariables:{
          AWS_REGION:$region,
          TALLY_PUBLIC_DEMO_ENABLED:"true",
          TALLY_PUBLIC_RATE_LIMIT_PER_MINUTE:"12",
          TALLY_PUBLIC_TIMEOUT_SECONDS:"20",
          TALLY_OAUTH_TOKEN_PARAMETER:$oauth,
          TALLY_OAUTH_REFRESH_LEASE_TABLE:$lease,
          TALLY_STATIC_DIR:"/app/ui"
        }
      }
    },
    AutoDeploymentsEnabled:false,
    AuthenticationConfiguration:{AccessRoleArn:$access}
  }')

SERVICE_ARN=$(aws apprunner list-services --profile "$PROFILE" --region "$REGION" \
  --query "ServiceSummaryList[?ServiceName=='${SERVICE_NAME}'].ServiceArn | [0]" --output text)
if [ "$SERVICE_ARN" = "None" ] || [ -z "$SERVICE_ARN" ]; then
  CREATE_OUTPUT=$(aws apprunner create-service --profile "$PROFILE" --region "$REGION" \
    --service-name "$SERVICE_NAME" \
    --source-configuration "$SOURCE_CONFIG" \
    --instance-configuration "Cpu=0.25 vCPU,Memory=0.5 GB,InstanceRoleArn=${INSTANCE_ROLE_ARN}" \
    --auto-scaling-configuration-arn "$AUTOSCALING_ARN" \
    --health-check-configuration 'Protocol=HTTP,Path=/readyz,Interval=10,Timeout=5,HealthyThreshold=1,UnhealthyThreshold=3' \
    --network-configuration 'IngressConfiguration={IsPubliclyAccessible=true}' \
    --tags Key=Project,Value=Tally Key=Environment,Value=synthetic-gate5 \
      Key=TeardownDate,Value=2026-09-30)
  SERVICE_ARN=$(jq -r '.Service.ServiceArn' <<<"$CREATE_OUTPUT")
else
  aws apprunner update-service --profile "$PROFILE" --region "$REGION" \
    --service-arn "$SERVICE_ARN" \
    --source-configuration "$SOURCE_CONFIG" \
    --instance-configuration "Cpu=0.25 vCPU,Memory=0.5 GB,InstanceRoleArn=${INSTANCE_ROLE_ARN}" \
    --auto-scaling-configuration-arn "$AUTOSCALING_ARN" \
    --health-check-configuration 'Protocol=HTTP,Path=/readyz,Interval=10,Timeout=5,HealthyThreshold=1,UnhealthyThreshold=3' \
    --network-configuration 'IngressConfiguration={IsPubliclyAccessible=true}' >/dev/null
fi

STATUS=""
for attempt in $(seq 1 60); do
  STATUS=$(aws apprunner describe-service --profile "$PROFILE" --region "$REGION" \
    --service-arn "$SERVICE_ARN" --query 'Service.Status' --output text)
  if [ "$STATUS" = "RUNNING" ]; then
    break
  fi
  if [ "$STATUS" = "CREATE_FAILED" ] || [ "$STATUS" = "DELETE_FAILED" ]; then
    echo "STOP: App Runner entered a failed state" >&2
    exit 1
  fi
  sleep 10
done
if [ "$STATUS" != "RUNNING" ]; then
  echo "STOP: App Runner did not reach RUNNING" >&2
  exit 1
fi

LIVE=$(aws apprunner describe-service --profile "$PROFILE" --region "$REGION" \
  --service-arn "$SERVICE_ARN" --output json)
if [ "$(jq -r '.Service.SourceConfiguration.ImageRepository.ImageIdentifier' <<<"$LIVE")" != "${ECR_URI}:${IMAGE_TAG}" ] \
  || [ "$(jq -r '.Service.HealthCheckConfiguration.Protocol' <<<"$LIVE")" != "HTTP" ] \
  || [ "$(jq -r '.Service.HealthCheckConfiguration.Path' <<<"$LIVE")" != "/readyz" ] \
  || [ "$(jq -r '.Service.AutoScalingConfigurationSummary.AutoScalingConfigurationArn' <<<"$LIVE")" != "$AUTOSCALING_ARN" ] \
  || [ "$(jq -r '.Service.NetworkConfiguration.IngressConfiguration.IsPubliclyAccessible' <<<"$LIVE")" != "true" ]; then
  echo "STOP: App Runner readback did not match deployment intent" >&2
  exit 1
fi
if jq -e '
  .Service.SourceConfiguration.ImageRepository.ImageConfiguration.RuntimeEnvironmentSecrets
  | has("TALLY_MCP_ACCESS_TOKEN") or has("TALLY_MCP_SERVICE_IDENTITY")
' >/dev/null <<<"$LIVE"; then
  echo "STOP: deployment retained a prohibited static MCP credential mapping" >&2
  exit 1
fi
if [ "$(jq -r '.Service.SourceConfiguration.ImageRepository.ImageConfiguration.RuntimeEnvironmentVariables.TALLY_OAUTH_TOKEN_PARAMETER' <<<"$LIVE")" != "$OAUTH_PARAMETER" ] \
  || [ "$(jq -r '.Service.SourceConfiguration.ImageRepository.ImageConfiguration.RuntimeEnvironmentVariables.TALLY_OAUTH_REFRESH_LEASE_TABLE' <<<"$LIVE")" != "$LEASE_TABLE_NAME" ]; then
  echo "STOP: deployment did not retain the OAuth token-provider configuration" >&2
  exit 1
fi

SERVICE_URL=$(jq -r '.Service.ServiceUrl' <<<"$LIVE")
curl --fail --silent --show-error --max-time 20 "https://${SERVICE_URL}/" >/dev/null
curl --fail --silent --show-error --max-time 20 "https://${SERVICE_URL}/readyz" >/dev/null
HERO=$(curl --fail --silent --show-error --max-time 30 "https://${SERVICE_URL}/public/demo/hero")
if ! jq -e '
  .classification == "SYNTHETIC DEMO — FICTIONAL DATA" and
  .status == "executed" and .mock_fallback == false and
  .replay.then.state == "FILED" and .replay.now.state == "CONTESTED" and
  .replay.receipt.bindings_unchanged == true and
  .replay.receipt.exact_versioned_s3_verified == true and
  .managed_mcp.status == "verified_read"
' >/dev/null <<<"$HERO"; then
  echo "STOP: deployed public hero did not satisfy the fixed live contract" >&2
  exit 1
fi

# Second invocation binds the verified service ARN into the one-time teardown.
scripts/gate5_guardrails.sh >/dev/null

echo "Gate 5 App Runner deployment verified"
echo "Demo URL: https://${SERVICE_URL}/"
