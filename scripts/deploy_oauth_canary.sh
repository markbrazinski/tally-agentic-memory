#!/usr/bin/env bash
# Deploy the OAuth refresh canary: a Lambda on a 4h EventBridge schedule that
# refreshes the Managed MCP OAuth bundle before it can lapse. Narrowly scoped
# (read+write only the one SSM oauth param, the one DynamoDB lease table, its own
# logs, KMS for the SecureString). Ends with a read-back assertion.
#
# Idempotent: re-running updates the function code + schedule in place.
set -euo pipefail

PROFILE="${AWS_PROFILE:-gate5-deployer}"
REGION="${AWS_REGION:-us-east-1}"
ACCOUNT="$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text)"

FN="tally-oauth-refresh-canary"
ROLE="tally-oauth-canary-role"
# The access token lives ~60 min, so the refresh MUST fire more often than that
# or it lapses for the rest of the hour (the 4h rate left it dead ~75% of the
# time). 45 min keeps a valid token continuously. Resource name kept for
# idempotent re-deploys (it is just a label, not the interval).
SCHEDULE="tally-oauth-canary-4h"
RATE="rate(45 minutes)"

OAUTH_PARAM="/tally/gate5/oauth-token-bundle"
LEASE_TABLE="tally-gate5-oauth-refresh-lease"
OAUTH_PARAM_ARN="arn:aws:ssm:${REGION}:${ACCOUNT}:parameter${OAUTH_PARAM}"
LEASE_TABLE_ARN="arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/${LEASE_TABLE}"
ROLE_ARN="arn:aws:iam::${ACCOUNT}:role/${ROLE}"

echo "==> 1/5  IAM role for the canary Lambda"
aws iam get-role --role-name "$ROLE" --profile "$PROFILE" >/dev/null 2>&1 || \
  aws iam create-role --role-name "$ROLE" --profile "$PROFILE" \
    --assume-role-policy-document '{
      "Version":"2012-10-17",
      "Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]
    }' >/dev/null

# Narrow inline policy: read+write ONLY the oauth param + lease table, KMS for the
# SecureString, and its own log group. No broad ssm:*/dynamodb:*.
KMS_KEY_ARN="$(aws ssm get-parameter --name "$OAUTH_PARAM" --profile "$PROFILE" --region "$REGION" \
  --query 'Parameter.ARN' --output text >/dev/null 2>&1 && echo "alias/aws/ssm" || echo "alias/aws/ssm")"
cat > /tmp/canary-policy.json <<JSON
{
  "Version":"2012-10-17",
  "Statement":[
    {"Sid":"OAuthParamRW","Effect":"Allow",
     "Action":["ssm:GetParameter","ssm:PutParameter"],
     "Resource":"${OAUTH_PARAM_ARN}"},
    {"Sid":"KmsForSecureString","Effect":"Allow",
     "Action":["kms:Decrypt","kms:Encrypt","kms:GenerateDataKey"],
     "Resource":"*",
     "Condition":{"StringEquals":{"kms:ViaService":"ssm.${REGION}.amazonaws.com"}}},
    {"Sid":"RefreshLeaseRW","Effect":"Allow",
     "Action":["dynamodb:GetItem","dynamodb:PutItem","dynamodb:UpdateItem","dynamodb:DeleteItem"],
     "Resource":"${LEASE_TABLE_ARN}"},
    {"Sid":"OwnLogs","Effect":"Allow",
     "Action":["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"],
     "Resource":"arn:aws:logs:${REGION}:${ACCOUNT}:log-group:/aws/lambda/${FN}:*"}
  ]
}
JSON
aws iam put-role-policy --role-name "$ROLE" --policy-name "${ROLE}-inline" \
  --policy-document file:///tmp/canary-policy.json --profile "$PROFILE" >/dev/null
echo "    role scoped to ${OAUTH_PARAM} + ${LEASE_TABLE} + own logs"

echo "==> 2/5  Package the Lambda (src/ + httpx)"
BUILD="$(mktemp -d)"
cp -R src "$BUILD/src"
# Lambda entrypoint shim at the package root.
cat > "$BUILD/lambda_function.py" <<'PY'
from src.platform.oauth_refresh_canary import handler as _handler
def handler(event=None, context=None):
    return _handler(event, context)
PY
# Vendor httpx (+ its deps) into the package; boto3 is provided by the runtime.
python3 -m pip install --quiet --target "$BUILD" "httpx==0.28.1" >/dev/null
( cd "$BUILD" && zip -qr /tmp/oauth-canary.zip . )
echo "    packaged $(du -h /tmp/oauth-canary.zip | cut -f1)"

echo "==> 3/5  Create/update the Lambda function"
# AWS_REGION is auto-provided by the Lambda runtime; the canary reads it natively.
ENV="Variables={TALLY_OAUTH_TOKEN_PARAMETER=${OAUTH_PARAM},TALLY_OAUTH_REFRESH_LEASE_TABLE=${LEASE_TABLE}}"
if aws lambda get-function --function-name "$FN" --profile "$PROFILE" --region "$REGION" >/dev/null 2>&1; then
  aws lambda update-function-code --function-name "$FN" \
    --zip-file fileb:///tmp/oauth-canary.zip --profile "$PROFILE" --region "$REGION" >/dev/null
  aws lambda wait function-updated --function-name "$FN" --profile "$PROFILE" --region "$REGION"
  aws lambda update-function-configuration --function-name "$FN" \
    --handler lambda_function.handler --timeout 60 --environment "$ENV" \
    --profile "$PROFILE" --region "$REGION" >/dev/null
else
  # Role propagation can lag; retry create briefly.
  for _ in $(seq 1 6); do
    aws lambda create-function --function-name "$FN" \
      --runtime python3.12 --handler lambda_function.handler --timeout 60 \
      --role "$ROLE_ARN" --zip-file fileb:///tmp/oauth-canary.zip \
      --environment "$ENV" --profile "$PROFILE" --region "$REGION" >/dev/null && break
    sleep 5
  done
fi
aws lambda wait function-updated --function-name "$FN" --profile "$PROFILE" --region "$REGION"
FN_ARN="$(aws lambda get-function --function-name "$FN" --profile "$PROFILE" --region "$REGION" \
  --query 'Configuration.FunctionArn' --output text)"

echo "==> 4/5  EventBridge schedule (${RATE})"
aws events put-rule --name "$SCHEDULE" --schedule-expression "$RATE" \
  --state ENABLED --profile "$PROFILE" --region "$REGION" >/dev/null
aws lambda add-permission --function-name "$FN" --statement-id "${SCHEDULE}-invoke" \
  --action lambda:InvokeFunction --principal events.amazonaws.com \
  --source-arn "arn:aws:events:${REGION}:${ACCOUNT}:rule/${SCHEDULE}" \
  --profile "$PROFILE" --region "$REGION" >/dev/null 2>&1 || true
aws events put-targets --rule "$SCHEDULE" \
  --targets "Id=canary,Arn=${FN_ARN}" --profile "$PROFILE" --region "$REGION" >/dev/null

echo "==> 5/5  Read-back assertions (intent == live)"
LIVE_RATE="$(aws events describe-rule --name "$SCHEDULE" --profile "$PROFILE" --region "$REGION" --query 'ScheduleExpression' --output text)"
LIVE_STATE="$(aws events describe-rule --name "$SCHEDULE" --profile "$PROFILE" --region "$REGION" --query 'State' --output text)"
LIVE_TARGET="$(aws events list-targets-by-rule --rule "$SCHEDULE" --profile "$PROFILE" --region "$REGION" --query 'Targets[0].Arn' --output text)"
LIVE_HANDLER="$(aws lambda get-function-configuration --function-name "$FN" --profile "$PROFILE" --region "$REGION" --query 'Handler' --output text)"
echo "    schedule: $LIVE_RATE / $LIVE_STATE ; target: $LIVE_TARGET ; handler: $LIVE_HANDLER"
[ "$LIVE_RATE" = "$RATE" ] || { echo "READ-BACK FAIL: rate $LIVE_RATE != $RATE"; exit 1; }
[ "$LIVE_STATE" = "ENABLED" ] || { echo "READ-BACK FAIL: schedule not ENABLED"; exit 1; }
[ "$LIVE_TARGET" = "$FN_ARN" ] || { echo "READ-BACK FAIL: target $LIVE_TARGET != $FN_ARN"; exit 1; }
[ "$LIVE_HANDLER" = "lambda_function.handler" ] || { echo "READ-BACK FAIL: handler $LIVE_HANDLER"; exit 1; }
echo "READ-BACK OK. NOTE: not 'shipped' until an unattended fire is observed in CloudWatch."
echo "Function: $FN_ARN"
rm -rf "$BUILD"
