#!/usr/bin/env bash
# Deploy the Tally capture Lambda. Zip deployment only (proper IaC deferred
# per docs/bundle-r.md). Uses the scoped `tally` AWS CLI profile — never
# default/root credentials (see CLAUDE.md AWS account section).
#
# Prerequisite (not created by this script): the Lambda execution role
# `example-archive-worker-lambda-exec` must already exist, with PutObject/GetObject
# on tally-demo-recordings* and basic CloudWatch Logs permissions. Create it once via:
#   aws iam create-role --profile example-profile --role-name example-archive-worker-lambda-exec \
#     --assume-role-policy-document file://iam/trust-policy-lambda.json
#   aws iam put-role-policy --profile example-profile --role-name example-archive-worker-lambda-exec \
#     --policy-name example-archive-worker-exec-inline --policy-document file://docs/aws/example-archive-worker-scoped-policy.json
# (trust-policy-lambda.json is the standard lambda.amazonaws.com AssumeRole doc.)
#
# Also a prerequisite: the EventBridge Scheduler needs its own execution
# role to invoke this Lambda (same role deploy_verifier.sh's schedule
# uses), e.g. `example-schedule-invoker-invoke-role` with lambda:InvokeFunction on
# tally-*. Create it once via:
#   aws iam create-role --profile example-profile --role-name example-schedule-invoker-invoke-role \
#     --assume-role-policy-document file://iam/trust-policy-scheduler.json
#   aws iam put-role-policy --profile example-profile --role-name example-schedule-invoker-invoke-role \
#     --policy-name example-schedule-invoker-invoke-inline --policy-document file://iam/scheduler-invoke-policy.json
set -euo pipefail

FUNCTION_NAME="example-archive-worker"
REGION="${AWS_REGION:-us-east-1}"
PROFILE="${AWS_PROFILE:-example-profile}"
BUCKET="${TALLY_BUCKET:-tally-demo-recordings}"
ROLE_ARN="arn:aws:iam::000000000000:role/example-archive-worker-lambda-exec"
SCHEDULER_ROLE_ARN="arn:aws:iam::000000000000:role/example-schedule-invoker-invoke-role"
BUILD_DIR="deploy/build"
ZIP_PATH="deploy/example-archive-worker.zip"
DAILY_SCHEDULE_NAME="example-archive-worker-daily-08utc"
RETRY_SCHEDULE_NAME="example-archive-worker-retry-09utc"

echo "== Building deployment package =="
rm -rf "$BUILD_DIR" "$ZIP_PATH"
mkdir -p "$BUILD_DIR"
# --platform/--python-version/--only-binary force pip to fetch wheels built
# for the Lambda runtime (Amazon Linux, x86_64, Python 3.12), not whatever
# platform this script happens to run on (e.g. macOS ARM64 locally) - psycopg
# ships a compiled C extension (psycopg-binary), and a wheel built for the
# wrong platform fails at Lambda import time with no local warning, since
# `pip install` on a dev machine silently picks the dev machine's own wheel.
python3 -m pip install -r requirements.txt --target "$BUILD_DIR" --quiet \
  --platform manylinux2014_x86_64 --python-version 312 --only-binary=:all: --implementation cp
cp -r capture "$BUILD_DIR/"
cp -r src "$BUILD_DIR/"
cp -r recording "$BUILD_DIR/"
(cd "$BUILD_DIR" && zip -r "../../$ZIP_PATH" . --quiet)

echo "== Deploying $FUNCTION_NAME (profile=$PROFILE region=$REGION) =="
if aws lambda get-function --profile "$PROFILE" --region "$REGION" --function-name "$FUNCTION_NAME" >/dev/null 2>&1; then
  aws lambda update-function-code --profile "$PROFILE" --region "$REGION" \
    --function-name "$FUNCTION_NAME" --zip-file "fileb://$ZIP_PATH"
  # A code update must fully finish propagating before the config update can
  # run - firing both back-to-back races and update-function-configuration
  # fails with ResourceConflictException ("update in progress").
  aws lambda wait function-updated --profile "$PROFILE" --region "$REGION" \
    --function-name "$FUNCTION_NAME"
  aws lambda update-function-configuration --profile "$PROFILE" --region "$REGION" \
    --function-name "$FUNCTION_NAME" --timeout 90 --memory-size 256 \
    --environment "Variables={TALLY_BUCKET=$BUCKET}"
else
  aws lambda create-function --profile "$PROFILE" --region "$REGION" \
    --function-name "$FUNCTION_NAME" --runtime python3.12 --handler capture.handler.lambda_handler \
    --role "$ROLE_ARN" --timeout 90 --memory-size 256 \
    --environment "Variables={TALLY_BUCKET=$BUCKET}" \
    --zip-file "fileb://$ZIP_PATH"
fi

echo "== Creating/updating EventBridge Scheduler rules (08:00 UTC daily + 09:00 UTC retry) =="
# docs/bundle-r.md Session 1: "EventBridge Scheduler: 08:00 UTC daily +
# 09:00 UTC retry rule (retry no-ops if today's manifest exists)". The
# retry rule invokes the SAME Lambda/handler - capture_one_source's own
# manifest_exists() check is what makes a 09:00 re-run a no-op for sources
# already captured at 08:00, per the whole-run-idempotent design
# (bundle-r.md Session 2 pre-flight Q2's own recommendation).
#
# Target.Input carries {"invocation":"scheduled"} (Bundle R addendum,
# 2026-07-05): lambda_handler defaults event.get("invocation") to "manual"
# when this key is absent, so every OTHER trigger (a bare `aws lambda
# invoke`, the console's Test button, local dev) is correctly disclosed as
# manual in the manifest/recordings row it produces - this is what makes
# the schedule's own unattended fires distinguishable after the fact from
# a human kicking off a run by hand.
FUNCTION_ARN=$(aws lambda get-function --profile "$PROFILE" --region "$REGION" \
  --function-name "$FUNCTION_NAME" --query "Configuration.FunctionArn" --output text)
SCHEDULED_INPUT='{"invocation":"scheduled"}'

for SCHEDULE_SPEC in "$DAILY_SCHEDULE_NAME:cron(0 8 * * ? *)" "$RETRY_SCHEDULE_NAME:cron(0 9 * * ? *)"; do
  SCHEDULE_NAME="${SCHEDULE_SPEC%%:*}"
  CRON_EXPR="${SCHEDULE_SPEC#*:}"
  if aws scheduler get-schedule --profile "$PROFILE" --region "$REGION" --name "$SCHEDULE_NAME" >/dev/null 2>&1; then
    aws scheduler update-schedule --profile "$PROFILE" --region "$REGION" \
      --name "$SCHEDULE_NAME" \
      --schedule-expression "$CRON_EXPR" \
      --flexible-time-window "Mode=OFF" \
      --target "{\"Arn\":\"$FUNCTION_ARN\",\"RoleArn\":\"$SCHEDULER_ROLE_ARN\",\"Input\":\"$(echo "$SCHEDULED_INPUT" | sed 's/"/\\"/g')\"}"
  else
    aws scheduler create-schedule --profile "$PROFILE" --region "$REGION" \
      --name "$SCHEDULE_NAME" \
      --schedule-expression "$CRON_EXPR" \
      --flexible-time-window "Mode=OFF" \
      --target "{\"Arn\":\"$FUNCTION_ARN\",\"RoleArn\":\"$SCHEDULER_ROLE_ARN\",\"Input\":\"$(echo "$SCHEDULED_INPUT" | sed 's/"/\\"/g')\"}"
  fi
done

# Read-back assertion (CLAUDE.md standing lock, Bundle R addendum #2:
# "a deploy isn't done when the API call succeeds - it's done when the
# script reads back live state and asserts it matches intent"). This is
# exactly the gap that let July 5's schedules run for hours with no
# Input at all: the create/update calls above had already returned
# success on an EARLIER run, so nothing failed loudly when a later
# commit's fix never actually got redeployed. Read the live Target back
# from AWS itself (not from this script's own variables) and fail the
# whole deploy if it doesn't match what was just requested.
echo "== Verifying live schedule state matches intent =="
for SCHEDULE_NAME in "$DAILY_SCHEDULE_NAME" "$RETRY_SCHEDULE_NAME"; do
  LIVE_INPUT=$(aws scheduler get-schedule --profile "$PROFILE" --region "$REGION" \
    --name "$SCHEDULE_NAME" --query "Target.Input" --output text)
  if [ "$LIVE_INPUT" != "$SCHEDULED_INPUT" ]; then
    echo "DEPLOY FAILED: $SCHEDULE_NAME's live Target.Input is '$LIVE_INPUT', expected '$SCHEDULED_INPUT'" >&2
    exit 1
  fi
  echo "  $SCHEDULE_NAME: Target.Input confirmed live ($LIVE_INPUT)"
done

echo "== Done =="
