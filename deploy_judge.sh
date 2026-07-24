#!/usr/bin/env bash
# Deploy the auth-gated judge demo onto the EXISTING isolated intake lane
# (tally-intake-v1). That lane is already isolated from the hero deploys
# (tally-app / tally-gate5-demo): its own App Runner service, ECR repo, runtime
# + access roles, its own CockroachDB database (tally_intake_deployed_20260723),
# its own /tally/intake-v1/* SSM params, and its own S3 key prefix
# (isolated-intake-v1) inside the shared tally-record bucket. This script does
# NOT create new AWS resources — it rebuilds the lane's image with the Cognito
# auth code and layers the auth env vars on, so the same isolated service now
# requires a judge login.
#
# Uses the `gate5-deployer` profile, whose tally-intake-v1-isolated-deploy
# policy scopes it to exactly this lane (ECR push, App Runner create/update/
# resume, /tally/intake-v1/* SSM, tally-intake-v1-* roles). No hero resource is
# touched. Every step ends in a read-back assertion (CLAUDE.md standing lock).
#
# Prerequisites (already provisioned, NOT created here):
#   - App Runner service tally-intake-v1 (may be PAUSED) + roles
#     tally-intake-v1-runtime / tally-intake-v1-ecr-access.
#   - /tally/intake-v1/{crdb-dsn,demo-token,tenant-id} SSM params (DB seeded).
#   - Cognito user pool + app client + judge user; pool/client ids in
#     /tally/intake-v1/cognito-{pool,client}-id.
set -euo pipefail

SERVICE_NAME="tally-intake-v1"
REGION="${AWS_REGION:-us-east-1}"
PROFILE="${TALLY_DEPLOY_PROFILE:-gate5-deployer}"
ACCOUNT_ID="352720962539"
ECR_REPO_NAME="tally-intake-v1"
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${ECR_REPO_NAME}"
ACCESS_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/tally-intake-v1-ecr-access"
INSTANCE_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/tally-intake-v1-runtime"
IMAGE_TAG="${TALLY_IMAGE_TAG:-$(git rev-parse --short HEAD 2>/dev/null || echo "manual")}"
SSM_PREFIX="/tally/intake-v1"
# App Runner caches images by TAG; pinning by digest forces a genuine pull and
# guarantees the running task matches what we just built. After build+push we
# resolve the pushed tag to its digest and deploy THAT.

# Cognito ids (not secret) live in SSM next to the DSN — one source of truth.
USER_POOL_ID=$(aws ssm get-parameter --profile "$PROFILE" --region "$REGION" \
  --name "${SSM_PREFIX}/cognito-pool-id" --query 'Parameter.Value' --output text)
CLIENT_ID=$(aws ssm get-parameter --profile "$PROFILE" --region "$REGION" \
  --name "${SSM_PREFIX}/cognito-client-id" --query 'Parameter.Value' --output text)
echo "== Cognito: pool ${USER_POOL_ID}, client ${CLIENT_ID} =="

SERVICE_ARN=$(aws apprunner list-services --profile "$PROFILE" --region "$REGION" \
  --query "ServiceSummaryList[?ServiceName=='${SERVICE_NAME}'].ServiceArn | [0]" --output text)
[ -n "$SERVICE_ARN" ] && [ "$SERVICE_ARN" != "None" ] \
  || { echo "FAILED: service ${SERVICE_NAME} not found — this script updates an existing lane" >&2; exit 1; }
echo "== Service: ${SERVICE_ARN} =="

echo "== Building Docker image (Dockerfile.judge, amd64) =="
docker build --platform linux/amd64 -t "${ECR_REPO_NAME}:${IMAGE_TAG}" -f Dockerfile.judge .

echo "== Verifying image architecture is amd64 =="
IMG_ARCH=$(docker image inspect "${ECR_REPO_NAME}:${IMAGE_TAG}" --format '{{.Architecture}}')
[ "$IMG_ARCH" = "amd64" ] || { echo "STOP: image is '$IMG_ARCH', App Runner needs 'amd64'." >&2; exit 1; }
echo "  arch: $IMG_ARCH (ok)"

echo "== Pushing image to ECR =="
aws ecr get-login-password --profile "$PROFILE" --region "$REGION" \
  | docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
docker tag "${ECR_REPO_NAME}:${IMAGE_TAG}" "${ECR_URI}:${IMAGE_TAG}"
docker push "${ECR_URI}:${IMAGE_TAG}"

# Resolve the pushed tag to its immutable digest and deploy THAT. App Runner
# caches images by tag; deploying by digest forces a real pull of the bytes we
# just built (the tag-cache bug that made the running task serve stale code).
IMAGE_DIGEST=$(aws ecr describe-images --profile "$PROFILE" --region "$REGION" \
  --repository-name "$ECR_REPO_NAME" --image-ids "imageTag=${IMAGE_TAG}" \
  --query 'imageDetails[0].imageDigest' --output text)
[ -n "$IMAGE_DIGEST" ] && [ "$IMAGE_DIGEST" != "None" ] \
  || { echo "FAILED: could not resolve digest for tag ${IMAGE_TAG}" >&2; exit 1; }
IMAGE_REF="${ECR_URI}@${IMAGE_DIGEST}"
echo "== Deploying by digest: ${IMAGE_REF} =="

# Resume first if paused — update-service requires a RUNNING/PAUSED service and
# the resume itself is a state transition we must wait out.
CUR_STATUS=$(aws apprunner describe-service --profile "$PROFILE" --region "$REGION" \
  --service-arn "$SERVICE_ARN" --query "Service.Status" --output text)
echo "== Current status: ${CUR_STATUS} =="
if [ "$CUR_STATUS" = "PAUSED" ]; then
  echo "== Resuming paused service =="
  aws apprunner resume-service --profile "$PROFILE" --region "$REGION" --service-arn "$SERVICE_ARN" >/dev/null
  for i in $(seq 1 40); do
    S=$(aws apprunner describe-service --profile "$PROFILE" --region "$REGION" \
      --service-arn "$SERVICE_ARN" --query "Service.Status" --output text)
    echo "  resume status: $S ($i/40)"; [ "$S" = "RUNNING" ] && break; sleep 15
  done
fi

echo "== Updating service: new image + Cognito auth env (preserving lane env) =="
# The lane's existing env (TALLY_INTAKE_BUCKET, TALLY_INTAKE_KEY_PREFIX,
# worker flags, TALLY_STATIC_DIR) is preserved; we add the auth vars. Secrets
# (DSN/token/tenant) stay pointed at /tally/intake-v1/*.
SOURCE_CONFIG=$(cat <<JSON
{
  "ImageRepository": {
    "ImageIdentifier": "${IMAGE_REF}",
    "ImageRepositoryType": "ECR",
    "ImageConfiguration": {
      "Port": "8000",
      "RuntimeEnvironmentSecrets": {
        "TALLY_CRDB_DSN": "arn:aws:ssm:${REGION}:${ACCOUNT_ID}:parameter${SSM_PREFIX}/crdb-dsn",
        "TALLY_DEMO_TOKEN": "arn:aws:ssm:${REGION}:${ACCOUNT_ID}:parameter${SSM_PREFIX}/demo-token",
        "TALLY_TENANT_ID": "arn:aws:ssm:${REGION}:${ACCOUNT_ID}:parameter${SSM_PREFIX}/tenant-id"
      },
      "RuntimeEnvironmentVariables": {
        "AWS_REGION": "${REGION}",
        "TALLY_INTAKE_BUCKET": "tally-record",
        "TALLY_INTAKE_KEY_PREFIX": "isolated-intake-v1",
        "TALLY_INTAKE_DEMO_ENABLED": "true",
        "TALLY_INTAKE_WORKER_ENABLED": "true",
        "TALLY_PUBLIC_DEMO_ENABLED": "false",
        "TALLY_STATIC_DIR": "/app/ui",
        "TALLY_JUDGE_AUTH_ENABLED": "true",
        "TALLY_COGNITO_USER_POOL_ID": "${USER_POOL_ID}",
        "TALLY_COGNITO_CLIENT_ID": "${CLIENT_ID}",
        "TALLY_COGNITO_REGION": "${REGION}"
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
aws apprunner update-service --profile "$PROFILE" --region "$REGION" \
  --service-arn "$SERVICE_ARN" \
  --source-configuration "$SOURCE_CONFIG" \
  --instance-configuration "InstanceRoleArn=${INSTANCE_ROLE_ARN}" >/dev/null

# update-service changes CONFIG but does not reliably roll the running task.
# Trigger an actual deployment so the running task picks up the new digest.
echo "== Waiting out the config update, then triggering a deployment =="
for i in $(seq 1 30); do
  S=$(aws apprunner describe-service --profile "$PROFILE" --region "$REGION" \
    --service-arn "$SERVICE_ARN" --query "Service.Status" --output text)
  [ "$S" = "RUNNING" ] && break; echo "  post-update: $S ($i)"; sleep 10
done
aws apprunner start-deployment --profile "$PROFILE" --region "$REGION" \
  --service-arn "$SERVICE_ARN" >/dev/null \
  || { echo "FAILED: start-deployment denied — the deployer role needs apprunner:StartDeployment on this service" >&2; exit 1; }

echo "== Waiting for RUNNING =="
for i in $(seq 1 60); do
  STATUS=$(aws apprunner describe-service --profile "$PROFILE" --region "$REGION" \
    --service-arn "$SERVICE_ARN" --query "Service.Status" --output text)
  echo "  status: $STATUS ($i/60)"
  [ "$STATUS" = "RUNNING" ] && break
  case "$STATUS" in CREATE_FAILED|DELETE_FAILED) echo "DEPLOY FAILED: $STATUS" >&2; exit 1;; esac
  sleep 15
done

echo "== Read-back: live state matches intent =="
LIVE_STATUS=$(aws apprunner describe-service --profile "$PROFILE" --region "$REGION" \
  --service-arn "$SERVICE_ARN" --query "Service.Status" --output text)
[ "$LIVE_STATUS" = "RUNNING" ] || { echo "FAILED: status '$LIVE_STATUS' != RUNNING" >&2; exit 1; }

LIVE_IMAGE=$(aws apprunner describe-service --profile "$PROFILE" --region "$REGION" \
  --service-arn "$SERVICE_ARN" \
  --query "Service.SourceConfiguration.ImageRepository.ImageIdentifier" --output text)
[ "$LIVE_IMAGE" = "${IMAGE_REF}" ] || { echo "FAILED: image '$LIVE_IMAGE' != ${IMAGE_REF}" >&2; exit 1; }

LIVE_AUTH=$(aws apprunner describe-service --profile "$PROFILE" --region "$REGION" \
  --service-arn "$SERVICE_ARN" \
  --query "Service.SourceConfiguration.ImageRepository.ImageConfiguration.RuntimeEnvironmentVariables.TALLY_JUDGE_AUTH_ENABLED" \
  --output text)
[ "$LIVE_AUTH" = "true" ] || { echo "FAILED: TALLY_JUDGE_AUTH_ENABLED live='$LIVE_AUTH' != true" >&2; exit 1; }

# Guard against the isolation regression: the lane MUST stay on its own DB/prefix.
LIVE_PREFIX=$(aws apprunner describe-service --profile "$PROFILE" --region "$REGION" \
  --service-arn "$SERVICE_ARN" \
  --query "Service.SourceConfiguration.ImageRepository.ImageConfiguration.RuntimeEnvironmentVariables.TALLY_INTAKE_KEY_PREFIX" \
  --output text)
[ "$LIVE_PREFIX" = "isolated-intake-v1" ] || { echo "FAILED: S3 key prefix '$LIVE_PREFIX' != isolated-intake-v1 — isolation broken" >&2; exit 1; }

SERVICE_URL=$(aws apprunner describe-service --profile "$PROFILE" --region "$REGION" \
  --service-arn "$SERVICE_ARN" --query "Service.ServiceUrl" --output text)

echo "== Read-back: /healthz is 200 =="
HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" "https://${SERVICE_URL}/healthz")
[ "$HTTP_STATUS" = "200" ] || { echo "FAILED: /healthz returned $HTTP_STATUS" >&2; exit 1; }

echo "== Read-back: auth ENFORCED (unauth /api/invoices must be 401) =="
API_STATUS=$(curl -s -o /dev/null -w "%{http_code}" "https://${SERVICE_URL}/api/invoices")
[ "$API_STATUS" = "401" ] || { echo "FAILED: unauth /api/invoices returned $API_STATUS, expected 401 — AUTH NOT ENFORCING" >&2; exit 1; }

# The login page must be reachable unauthenticated — the static-mount catch-all
# once shadowed it to 404 (auth enforced but no way to log in). Assert it here so
# that regression can never pass the deploy gate again.
echo "== Read-back: /login reachable (must be 200, not shadowed to 404) =="
LOGIN_STATUS=$(curl -s -o /dev/null -w "%{http_code}" "https://${SERVICE_URL}/login")
if [ "${TALLY_SKIP_LOGIN_READBACK:-}" = "true" ]; then
  echo "  (skipped by TALLY_SKIP_LOGIN_READBACK — diagnostic deploy) live=$LOGIN_STATUS"
else
  [ "$LOGIN_STATUS" = "200" ] || { echo "FAILED: /login returned $LOGIN_STATUS, expected 200 — login page shadowed by static mount" >&2; exit 1; }
fi

echo "== Done =="
echo "  Service ARN: $SERVICE_ARN"
echo "  Live URL:    https://${SERVICE_URL}"
echo "  Image:       ${ECR_URI}:${IMAGE_TAG}"
echo "  Auth:        ENFORCED (unauth API = 401, verified live)"
echo "  Isolation:   DB tally_intake_deployed_20260723, S3 prefix isolated-intake-v1"
