#!/usr/bin/env bash
# Deploy the Tally verifier Lambda + its CloudWatch alarm + SNS topic +
# 10:00 UTC EventBridge Scheduler rule. Companion to deploy.sh (the capture
# Lambda's own deploy script — NOT touched by this file). Zip deployment
# only (proper IaC deferred per docs/bundle-r.md, same as deploy.sh). Uses
# the scoped `tally` AWS CLI profile — never default/root credentials (see
# CLAUDE.md AWS account section).
#
# docs/bundle-r.md Session 2: "a scheduled verifier Lambda at 10:00 UTC that
# checks 'does today's manifest exist for every registered source?' and
# alarms ExampleRecoveryGap if not." This script provisions all four pieces:
# the Lambda itself, the SNS topic the alarm notifies, the CloudWatch alarm
# watching the ExampleTally/Capture:ExampleRecoveryGap metric the Lambda emits, and
# the 10:00 UTC schedule that invokes it daily — the schedule is treated as
# part of THIS deliverable (not separate infra) because bundle-r.md names
# the 10:00 UTC trigger as part of the verifier's own spec, the same way
# the capture Lambda's 08:00/09:00 schedules are understood to ship
# alongside capture/handler.py itself.
#
# Prerequisite (not created by this script): the Lambda execution role
# `example-audit-worker-lambda-exec` must already exist, with GetObject (read-only
# is enough — the verifier never writes to S3) on tally-demo-recordings*, PutMetricData
# on CloudWatch, and basic CloudWatch Logs permissions. Create it once via:
#   aws iam create-role --profile example-profile --role-name example-audit-worker-lambda-exec \
#     --assume-role-policy-document file://iam/trust-policy-lambda.json
#   aws iam put-role-policy --profile example-profile --role-name example-audit-worker-lambda-exec \
#     --policy-name example-audit-worker-exec-inline --policy-document file://docs/aws/example-archive-worker-scoped-policy.json
# (trust-policy-lambda.json is the standard lambda.amazonaws.com AssumeRole doc,
# same file deploy.sh's prerequisite comment references. Reusing the existing
# scoped policy is fine here: it already grants s3:GetObject on tally-demo-recordings*,
# cloudwatch:PutMetricData, and Lambda log-group permissions scoped to
# `/aws/lambda/example-*` — see the example scoped policy.)
#
# Also a prerequisite (not created by this script, requires operator credentials
# this script's IAM scope was never meant to hold): the EventBridge Scheduler
# needs its own execution role to invoke the Lambda target, e.g.
# `example-schedule-invoker-invoke-role` with lambda:InvokeFunction on example-audit-worker.
# Create it once via:
#   aws iam create-role --profile example-profile --role-name example-schedule-invoker-invoke-role \
#     --assume-role-policy-document file://iam/trust-policy-scheduler.json
#   aws iam put-role-policy --profile example-profile --role-name example-schedule-invoker-invoke-role \
#     --policy-name example-schedule-invoker-invoke-inline --policy-document file://iam/scheduler-invoke-policy.json
# (trust-policy-scheduler.json is the standard scheduler.amazonaws.com AssumeRole doc.)
set -euo pipefail

FUNCTION_NAME="example-audit-worker"
REGION="${AWS_REGION:-us-east-1}"
PROFILE="${AWS_PROFILE:-example-profile}"
BUCKET="${TALLY_BUCKET:-tally-demo-recordings}"
ROLE_ARN="arn:aws:iam::000000000000:role/example-audit-worker-lambda-exec"
SCHEDULER_ROLE_ARN="arn:aws:iam::000000000000:role/example-schedule-invoker-invoke-role"
BUILD_DIR="deploy/build-verifier"
ZIP_PATH="deploy/example-audit-worker.zip"

SNS_TOPIC_NAME="example-recovery-alerts"
ALARM_NAME="ExampleRecoveryGap"
METRIC_NAMESPACE="ExampleTally/Capture"
METRIC_NAME="ExampleRecoveryGap"
SCHEDULE_NAME="example-audit-worker-daily-10utc"

echo "== Building deployment package =="
rm -rf "$BUILD_DIR" "$ZIP_PATH"
mkdir -p "$BUILD_DIR"
# capture/verifier.py only imports from within capture/ (S3 reads + a
# CloudWatch metric put, no DB access) - no psycopg, no src/, no recording/
# needed, so no platform-specific wheel concern here (unlike deploy.sh's
# capture Lambda, which needs psycopg's compiled extension built for
# Lambda's actual runtime platform, not whatever platform this script runs
# on).
python3 -m pip install -r requirements.txt --target "$BUILD_DIR" --quiet
cp -r capture "$BUILD_DIR/"
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
    --function-name "$FUNCTION_NAME" --timeout 30 --memory-size 128 \
    --environment "Variables={TALLY_BUCKET=$BUCKET}"
else
  aws lambda create-function --profile "$PROFILE" --region "$REGION" \
    --function-name "$FUNCTION_NAME" --runtime python3.12 --handler capture.verifier.lambda_handler \
    --role "$ROLE_ARN" --timeout 30 --memory-size 128 \
    --environment "Variables={TALLY_BUCKET=$BUCKET}" \
    --zip-file "fileb://$ZIP_PATH"
fi

echo "== Ensuring SNS topic $SNS_TOPIC_NAME exists =="
# create-topic is idempotent: calling it again on an existing topic just
# returns the same ARN, so this is safe to re-run.
TOPIC_ARN=$(aws sns create-topic --profile "$PROFILE" --region "$REGION" \
  --name "$SNS_TOPIC_NAME" --query "TopicArn" --output text)
echo "Topic ARN: $TOPIC_ARN"
echo
echo "NOTE: no email subscription is created by this script (the operator's"
echo "email address is not something deploy tooling should invent or hardcode)."
echo "Subscribe once, manually, after this script runs:"
echo "  aws sns subscribe --profile $PROFILE --region $REGION \\"
echo "    --topic-arn $TOPIC_ARN --protocol email --notification-endpoint <your-email>"
echo "Then confirm the subscription via the email AWS sends."

echo "== Creating/updating CloudWatch alarm $ALARM_NAME =="
aws cloudwatch put-metric-alarm --profile "$PROFILE" --region "$REGION" \
  --alarm-name "$ALARM_NAME" \
  --alarm-description "Fires when the verifier Lambda finds any registered source missing or failed for today's manifest (docs/bundle-r.md Session 2)." \
  --namespace "$METRIC_NAMESPACE" \
  --metric-name "$METRIC_NAME" \
  --statistic Maximum \
  --period 3600 \
  --evaluation-periods 1 \
  --threshold 1 \
  --comparison-operator GreaterThanOrEqualToThreshold \
  --treat-missing-data breaching \
  --alarm-actions "$TOPIC_ARN"

echo "== Creating/updating EventBridge Scheduler rule $SCHEDULE_NAME (10:00 UTC daily) =="
# cron(0 10 * * ? *) = every day at 10:00 UTC, matching bundle-r.md's spec
# ("a scheduled verifier Lambda at 10:00 UTC") and following the same
# aws scheduler create-schedule pattern used for the capture Lambda's
# existing 08:00/09:00 schedules.
#
# Target.Input carries {"invocation":"scheduled"} (Bundle R addendum,
# 2026-07-05) - see capture/verifier.py's lambda_handler docstring: this is
# what lets a CloudWatch Logs check prove the verifier actually fired on
# its own schedule, rather than only ever having been invoked by hand.
VERIFIER_FUNCTION_ARN=$(aws lambda get-function --profile "$PROFILE" --region "$REGION" \
  --function-name "$FUNCTION_NAME" --query "Configuration.FunctionArn" --output text)
SCHEDULED_INPUT='{"invocation":"scheduled"}'

if aws scheduler get-schedule --profile "$PROFILE" --region "$REGION" --name "$SCHEDULE_NAME" >/dev/null 2>&1; then
  aws scheduler update-schedule --profile "$PROFILE" --region "$REGION" \
    --name "$SCHEDULE_NAME" \
    --schedule-expression "cron(0 10 * * ? *)" \
    --flexible-time-window "Mode=OFF" \
    --target "{\"Arn\":\"$VERIFIER_FUNCTION_ARN\",\"RoleArn\":\"$SCHEDULER_ROLE_ARN\",\"Input\":\"$(echo "$SCHEDULED_INPUT" | sed 's/"/\\"/g')\"}"
else
  aws scheduler create-schedule --profile "$PROFILE" --region "$REGION" \
    --name "$SCHEDULE_NAME" \
    --schedule-expression "cron(0 10 * * ? *)" \
    --flexible-time-window "Mode=OFF" \
    --target "{\"Arn\":\"$VERIFIER_FUNCTION_ARN\",\"RoleArn\":\"$SCHEDULER_ROLE_ARN\",\"Input\":\"$(echo "$SCHEDULED_INPUT" | sed 's/"/\\"/g')\"}"
fi

# Read-back assertion (CLAUDE.md standing lock, Bundle R addendum #2:
# "a deploy isn't done when the API call succeeds - it's done when the
# script reads back live state and asserts it matches intent"). Same
# gap that silently affected deploy.sh's schedules also applied here -
# read the live Target back from AWS itself and fail loudly on mismatch.
echo "== Verifying live schedule state matches intent =="
LIVE_INPUT=$(aws scheduler get-schedule --profile "$PROFILE" --region "$REGION" \
  --name "$SCHEDULE_NAME" --query "Target.Input" --output text)
if [ "$LIVE_INPUT" != "$SCHEDULED_INPUT" ]; then
  echo "DEPLOY FAILED: $SCHEDULE_NAME's live Target.Input is '$LIVE_INPUT', expected '$SCHEDULED_INPUT'" >&2
  exit 1
fi
echo "  $SCHEDULE_NAME: Target.Input confirmed live ($LIVE_INPUT)"

echo "== Done =="
