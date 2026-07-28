#!/usr/bin/env bash
# RUN IN AWS CLOUDSHELL (your admin creds). Deploys the OAuth refresh canary:
# a Lambda on a 4h EventBridge schedule that refreshes the Managed MCP OAuth
# bundle before it can lapse. Self-contained: clones the repo, packages the
# Lambda, creates a NARROWLY-scoped role, the function, and the schedule, then
# reads back live state. Nothing is granted to the day-to-day deploy profile.
#
# Prereq: seed a VALID token first (interactive bootstrap) — the canary keeps a
# live token alive, it cannot resurrect a dead one. Order is fine either way for
# DEPLOY; the first fire just fails-loud until the token is valid.
set -euo pipefail

REGION="us-east-1"
ACCOUNT="352720962539"
REPO_URL="https://github.com/markbrazinski/tally-agentic-memory.git"
BRANCH="authority-transition-v1"

FN="tally-oauth-refresh-canary"
ROLE="tally-oauth-canary-role"
SCHEDULE="tally-oauth-canary-4h"
RATE="rate(4 hours)"
OAUTH_PARAM="/tally/gate5/oauth-token-bundle"
LEASE_TABLE="tally-gate5-oauth-refresh-lease"
OAUTH_PARAM_ARN="arn:aws:ssm:${REGION}:${ACCOUNT}:parameter${OAUTH_PARAM}"
LEASE_TABLE_ARN="arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/${LEASE_TABLE}"
ROLE_ARN="arn:aws:iam::${ACCOUNT}:role/${ROLE}"

echo "==> 0/6  Clone the repo (branch ${BRANCH})"
WORK="$(mktemp -d)"; cd "$WORK"
git clone --depth 1 --branch "$BRANCH" "$REPO_URL" repo >/dev/null 2>&1 || {
  echo "clone failed — is the repo private? If so run:  gh auth login  (or use a PAT in REPO_URL)"; exit 1; }
cd repo

echo "==> 1/6  Narrowly-scoped IAM role"
aws iam get-role --role-name "$ROLE" >/dev/null 2>&1 || \
  aws iam create-role --role-name "$ROLE" \
    --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
cat > /tmp/canary-policy.json <<JSON
{"Version":"2012-10-17","Statement":[
  {"Sid":"OAuthParamRW","Effect":"Allow","Action":["ssm:GetParameter","ssm:PutParameter"],"Resource":"${OAUTH_PARAM_ARN}"},
  {"Sid":"KmsViaSsm","Effect":"Allow","Action":["kms:Decrypt","kms:Encrypt","kms:GenerateDataKey"],"Resource":"*","Condition":{"StringEquals":{"kms:ViaService":"ssm.${REGION}.amazonaws.com"}}},
  {"Sid":"RefreshLeaseRW","Effect":"Allow","Action":["dynamodb:GetItem","dynamodb:PutItem","dynamodb:UpdateItem","dynamodb:DeleteItem"],"Resource":"${LEASE_TABLE_ARN}"},
  {"Sid":"OwnLogs","Effect":"Allow","Action":["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"],"Resource":"arn:aws:logs:${REGION}:${ACCOUNT}:log-group:/aws/lambda/${FN}:*"}
]}
JSON
aws iam put-role-policy --role-name "$ROLE" --policy-name "${ROLE}-inline" --policy-document file:///tmp/canary-policy.json >/dev/null
echo "    role scoped to ${OAUTH_PARAM} + ${LEASE_TABLE} + own logs only"

echo "==> 2/6  Package the Lambda (src/ + httpx)"
BUILD="$(mktemp -d)"
cp -R src "$BUILD/src"
cat > "$BUILD/lambda_function.py" <<'PY'
from src.platform.oauth_refresh_canary import handler as _handler
def handler(event=None, context=None):
    return _handler(event, context)
PY
python3 -m pip install --quiet --target "$BUILD" "httpx==0.28.1" >/dev/null
( cd "$BUILD" && zip -qr /tmp/oauth-canary.zip . )
echo "    packaged $(du -h /tmp/oauth-canary.zip | cut -f1)"

echo "==> 3/6  Create/update the Lambda"
ENV="Variables={TALLY_OAUTH_TOKEN_PARAMETER=${OAUTH_PARAM},TALLY_OAUTH_REFRESH_LEASE_TABLE=${LEASE_TABLE}}"
if aws lambda get-function --function-name "$FN" >/dev/null 2>&1; then
  aws lambda update-function-code --function-name "$FN" --zip-file fileb:///tmp/oauth-canary.zip >/dev/null
  aws lambda wait function-updated --function-name "$FN"
  aws lambda update-function-configuration --function-name "$FN" --handler lambda_function.handler --timeout 60 --environment "$ENV" >/dev/null
else
  for _ in $(seq 1 6); do
    aws lambda create-function --function-name "$FN" --runtime python3.12 --handler lambda_function.handler \
      --timeout 60 --role "$ROLE_ARN" --zip-file fileb:///tmp/oauth-canary.zip --environment "$ENV" >/dev/null && break
    echo "    (waiting for role to propagate...)"; sleep 5
  done
fi
aws lambda wait function-updated --function-name "$FN"
FN_ARN="$(aws lambda get-function --function-name "$FN" --query 'Configuration.FunctionArn' --output text)"

echo "==> 4/6  EventBridge 4h schedule"
aws events put-rule --name "$SCHEDULE" --schedule-expression "$RATE" --state ENABLED >/dev/null
aws lambda add-permission --function-name "$FN" --statement-id "${SCHEDULE}-invoke" \
  --action lambda:InvokeFunction --principal events.amazonaws.com \
  --source-arn "arn:aws:events:${REGION}:${ACCOUNT}:rule/${SCHEDULE}" >/dev/null 2>&1 || true
aws events put-targets --rule "$SCHEDULE" --targets "Id=canary,Arn=${FN_ARN}" >/dev/null

echo "==> 5/6  Invoke once now (proves wiring; also refreshes if token is valid)"
aws lambda invoke --function-name "$FN" --payload '{}' /tmp/canary-out.json >/dev/null 2>&1 || true
echo "    result: $(cat /tmp/canary-out.json 2>/dev/null || echo '(see CloudWatch)')"

echo "==> 6/6  Read-back assertions"
LIVE_RATE="$(aws events describe-rule --name "$SCHEDULE" --query 'ScheduleExpression' --output text)"
LIVE_STATE="$(aws events describe-rule --name "$SCHEDULE" --query 'State' --output text)"
LIVE_TARGET="$(aws events list-targets-by-rule --rule "$SCHEDULE" --query 'Targets[0].Arn' --output text)"
echo "    schedule=$LIVE_RATE state=$LIVE_STATE target=$LIVE_TARGET"
[ "$LIVE_RATE" = "$RATE" ] && [ "$LIVE_STATE" = "ENABLED" ] && [ "$LIVE_TARGET" = "$FN_ARN" ] \
  && echo "READ-BACK OK" || { echo "READ-BACK FAIL"; exit 1; }
echo ""
echo "DONE. The canary fires every 4h. It is not 'shipped' until an unattended"
echo "fire is observed in CloudWatch (/aws/lambda/${FN}). If step 5 returned an"
echo "oauth_refresh_failed error, the token is currently dead — re-mint once via"
echo "the interactive bootstrap and the next fire will take over."
