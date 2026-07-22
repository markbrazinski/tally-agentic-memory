#!/usr/bin/env bash
# Configure the cumulative $50 AWS notification budget and the one-time App
# Runner teardown action. The email address is supplied privately at execution
# time. This script never creates an AWS Budget Action or other automatic stop.
set -euo pipefail
umask 077

REGION="${AWS_REGION:-us-east-1}"
PROFILE="${AWS_PROFILE:?set AWS_PROFILE to the scoped Gate 5 deployer profile}"
ALERT_EMAIL="${TALLY_GATE5_BUDGET_EMAIL:?set the private budget-alert recipient}"
SERVICE_NAME="${TALLY_GATE5_SERVICE_NAME:-tally-gate5-demo}"
BUDGET_NAME="tally-gate5-total-10-usd"
BUDGET_LIMIT="50"
BUDGET_START="2026-07-01T00:00:00Z"
# Keep one non-resetting total while the teardown date may move in seven-day
# increments. The existing budget name is retained to avoid replacing a live
# guardrail merely because its original name included the superseded ceiling.
# This one-year end avoids requiring the account's optional multi-year cost
# history. It can be extended in place without resetting the cumulative total.
BUDGET_END="2027-06-30T00:00:00Z"
# The existing schedule name is also retained so updating the date cannot leave
# a second, earlier destructive schedule behind.
SCHEDULE_NAME="tally-gate5-teardown-2026-09-22"
INITIAL_TEARDOWN_DATE="2026-09-30"
TEARDOWN_DATE="${TALLY_GATE5_TEARDOWN_DATE:-${INITIAL_TEARDOWN_DATE}}"
TEARDOWN_EXPRESSION="at(${TEARDOWN_DATE}T23:59:00)"
SCHEDULER_ROLE_NAME="tally-gate5-teardown-scheduler"
PYTHON="${TALLY_PYTHON:-python3}"

if [[ ! "$ALERT_EMAIL" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]]; then
  echo "STOP: budget-alert recipient is not a valid email address" >&2
  exit 1
fi

if ! "$PYTHON" - "$TEARDOWN_DATE" <<'PY'
from datetime import date
import sys

initial = date.fromisoformat("2026-09-30")
candidate = date.fromisoformat(sys.argv[1])
delta = (candidate - initial).days
raise SystemExit(0 if delta >= 0 and delta % 7 == 0 else 1)
PY
then
  echo "STOP: teardown date must be 2026-09-30 or a later seven-day increment" >&2
  exit 1
fi

ACCOUNT_ID=$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text)
if [[ ! "$ACCOUNT_ID" =~ ^[0-9]{12}$ ]]; then
  echo "STOP: scoped AWS identity did not return a valid account" >&2
  exit 1
fi

TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT
cat >"$TMP_DIR/budget.json" <<JSON
{
  "BudgetName":"${BUDGET_NAME}",
  "BudgetLimit":{"Amount":"${BUDGET_LIMIT}","Unit":"USD"},
  "TimeUnit":"CUSTOM",
  "TimePeriod":{"Start":"${BUDGET_START}","End":"${BUDGET_END}"},
  "BudgetType":"COST",
  "CostFilters":{"Service":[
    "AWS App Runner",
    "Amazon Elastic Container Registry (ECR)",
    "Amazon DynamoDB",
    "AWS Systems Manager",
    "Amazon Simple Storage Service",
    "Amazon Bedrock",
    "Amazon EventBridge"
  ]},
  "CostTypes":{"IncludeTax":true,"IncludeSubscription":true,"UseBlended":false,"IncludeRefund":false,"IncludeCredit":false,"IncludeUpfront":true,"IncludeRecurring":true,"IncludeOtherSubscription":true,"IncludeSupport":true,"IncludeDiscount":true,"UseAmortized":false}
}
JSON

if aws budgets describe-budget --profile "$PROFILE" --account-id "$ACCOUNT_ID" \
  --budget-name "$BUDGET_NAME" >/dev/null 2>&1; then
  aws budgets update-budget --profile "$PROFILE" --account-id "$ACCOUNT_ID" \
    --new-budget "file://$TMP_DIR/budget.json" >/dev/null
else
  aws budgets create-budget --profile "$PROFILE" --account-id "$ACCOUNT_ID" \
    --budget "file://$TMP_DIR/budget.json" >/dev/null
fi

ensure_notification() {
  local threshold="$1"
  cat >"$TMP_DIR/notification.json" <<JSON
{"NotificationType":"ACTUAL","ComparisonOperator":"GREATER_THAN","Threshold":${threshold},"ThresholdType":"ABSOLUTE_VALUE"}
JSON
  cat >"$TMP_DIR/subscriber.json" <<JSON
[{"SubscriptionType":"EMAIL","Address":"${ALERT_EMAIL}"}]
JSON
  local notifications
  notifications=$(aws budgets describe-notifications-for-budget --profile "$PROFILE" \
    --account-id "$ACCOUNT_ID" --budget-name "$BUDGET_NAME" --output json)
  if ! jq -e --argjson threshold "$threshold" '
    any(.Notifications[];
      .NotificationType == "ACTUAL" and
      .ComparisonOperator == "GREATER_THAN" and
      .ThresholdType == "ABSOLUTE_VALUE" and
      .Threshold == $threshold)
  ' >/dev/null <<<"$notifications"; then
    aws budgets create-notification --profile "$PROFILE" --account-id "$ACCOUNT_ID" \
      --budget-name "$BUDGET_NAME" --notification "file://$TMP_DIR/notification.json" \
      --subscribers "file://$TMP_DIR/subscriber.json" >/dev/null
    return
  fi
  local subscribers
  subscribers=$(aws budgets describe-subscribers-for-notification --profile "$PROFILE" \
    --account-id "$ACCOUNT_ID" --budget-name "$BUDGET_NAME" \
    --notification "file://$TMP_DIR/notification.json" --output json)
  if ! jq -e --arg email "$ALERT_EMAIL" '
    any(.Subscribers[]; .SubscriptionType == "EMAIL" and .Address == $email)
  ' >/dev/null <<<"$subscribers"; then
    aws budgets create-subscriber --profile "$PROFILE" --account-id "$ACCOUNT_ID" \
      --budget-name "$BUDGET_NAME" --notification "file://$TMP_DIR/notification.json" \
      --subscriber "SubscriptionType=EMAIL,Address=${ALERT_EMAIL}" >/dev/null
  fi
}

# Delete only obsolete notifications on this budget. Deletion also removes
# their subscribers; each desired notification is then recreated/read back.
CURRENT_NOTIFICATIONS=$(aws budgets describe-notifications-for-budget --profile "$PROFILE" \
  --account-id "$ACCOUNT_ID" --budget-name "$BUDGET_NAME" --output json)
while IFS= read -r notification; do
  aws budgets delete-notification --profile "$PROFILE" --account-id "$ACCOUNT_ID" \
    --budget-name "$BUDGET_NAME" --notification "$notification" >/dev/null
done < <(jq -c '
  .Notifications[]
  | select((
      .NotificationType == "ACTUAL" and
      .ComparisonOperator == "GREATER_THAN" and
      .ThresholdType == "ABSOLUTE_VALUE" and
      (.Threshold as $threshold | [15, 25, 40, 50] | index($threshold) != null)
    ) | not)
' <<<"$CURRENT_NOTIFICATIONS")

ensure_notification 15
ensure_notification 25
ensure_notification 40
ensure_notification 50

BUDGET_AMOUNT=$(aws budgets describe-budget --profile "$PROFILE" --account-id "$ACCOUNT_ID" \
  --budget-name "$BUDGET_NAME" --query 'Budget.BudgetLimit.Amount' --output text)
BUDGET_TIME_UNIT=$(aws budgets describe-budget --profile "$PROFILE" --account-id "$ACCOUNT_ID" \
  --budget-name "$BUDGET_NAME" --query 'Budget.TimeUnit' --output text)
BUDGET_FILTERS=$(aws budgets describe-budget --profile "$PROFILE" --account-id "$ACCOUNT_ID" \
  --budget-name "$BUDGET_NAME" --query 'Budget.CostFilters.Service' --output json)
NOTIFICATIONS=$(aws budgets describe-notifications-for-budget --profile "$PROFILE" \
  --account-id "$ACCOUNT_ID" --budget-name "$BUDGET_NAME" --output json)
if ! jq -en --arg amount "$BUDGET_AMOUNT" '($amount | tonumber) == 50' >/dev/null \
  || [ "$BUDGET_TIME_UNIT" != "CUSTOM" ] \
  || ! jq -e '
    sort == [
      "AWS App Runner",
      "AWS Systems Manager",
      "Amazon Bedrock",
      "Amazon DynamoDB",
      "Amazon Elastic Container Registry (ECR)",
      "Amazon EventBridge",
      "Amazon Simple Storage Service"
    ]
  ' >/dev/null <<<"$BUDGET_FILTERS" \
  || ! jq -e '
    [.Notifications[] | {
      type: .NotificationType,
      operator: .ComparisonOperator,
      threshold_type: .ThresholdType,
      threshold: .Threshold
    }]
    | sort_by(.threshold)
    == [
      {type:"ACTUAL",operator:"GREATER_THAN",threshold_type:"ABSOLUTE_VALUE",threshold:15},
      {type:"ACTUAL",operator:"GREATER_THAN",threshold_type:"ABSOLUTE_VALUE",threshold:25},
      {type:"ACTUAL",operator:"GREATER_THAN",threshold_type:"ABSOLUTE_VALUE",threshold:40},
      {type:"ACTUAL",operator:"GREATER_THAN",threshold_type:"ABSOLUTE_VALUE",threshold:50}
    ]
  ' >/dev/null <<<"$NOTIFICATIONS"; then
  echo "STOP: scoped budget or alert readback did not match the \$50 ceiling" >&2
  exit 1
fi

# The budget must exist before App Runner. On the pre-deployment invocation,
# there is intentionally no service ARN to place in the one-time teardown job.
SERVICE_ARN=$(aws apprunner list-services --profile "$PROFILE" --region "$REGION" \
  --query "ServiceSummaryList[?ServiceName=='${SERVICE_NAME}'].ServiceArn | [0]" --output text)
if [ "$SERVICE_ARN" = "None" ] || [ -z "$SERVICE_ARN" ]; then
  echo "Gate 5 guardrails verified: budget_usd=50 alerts=4 teardown=pending-service"
  exit 0
fi

cat >"$TMP_DIR/scheduler-trust.json" <<'JSON'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"scheduler.amazonaws.com"},"Action":"sts:AssumeRole"}]}
JSON
if ! aws iam get-role --profile "$PROFILE" --role-name "$SCHEDULER_ROLE_NAME" >/dev/null 2>&1; then
  aws iam create-role --profile "$PROFILE" --role-name "$SCHEDULER_ROLE_NAME" \
    --assume-role-policy-document "file://$TMP_DIR/scheduler-trust.json" \
    --tags Key=Project,Value=Tally Key=Environment,Value=synthetic-gate5 \
      Key=TeardownDate,Value="$TEARDOWN_DATE" >/dev/null
fi
SCHEDULER_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${SCHEDULER_ROLE_NAME}"
jq -n --arg service "$SERVICE_ARN" \
  '{Version:"2012-10-17",Statement:[{Effect:"Allow",Action:"apprunner:DeleteService",Resource:$service}]}' \
  >"$TMP_DIR/scheduler-policy.json"
aws iam put-role-policy --profile "$PROFILE" --role-name "$SCHEDULER_ROLE_NAME" \
  --policy-name tally-gate5-delete-one-service \
  --policy-document "file://$TMP_DIR/scheduler-policy.json" >/dev/null

TARGET=$(jq -n --arg role "$SCHEDULER_ROLE_ARN" --arg service "$SERVICE_ARN" \
  '{Arn:"arn:aws:scheduler:::aws-sdk:apprunner:deleteService",RoleArn:$role,Input:({ServiceArn:$service}|tojson),RetryPolicy:{MaximumEventAgeInSeconds:3600,MaximumRetryAttempts:3}}')
if aws scheduler get-schedule --profile "$PROFILE" --region "$REGION" \
  --name "$SCHEDULE_NAME" >/dev/null 2>&1; then
  aws scheduler update-schedule --profile "$PROFILE" --region "$REGION" \
    --name "$SCHEDULE_NAME" --schedule-expression "$TEARDOWN_EXPRESSION" \
    --schedule-expression-timezone America/Los_Angeles \
    --flexible-time-window Mode=OFF --target "$TARGET" --state ENABLED \
    --action-after-completion DELETE >/dev/null
else
  aws scheduler create-schedule --profile "$PROFILE" --region "$REGION" \
    --name "$SCHEDULE_NAME" --description 'Delete synthetic Tally judge service after judging' \
    --schedule-expression "$TEARDOWN_EXPRESSION" \
    --schedule-expression-timezone America/Los_Angeles \
    --flexible-time-window Mode=OFF --target "$TARGET" --state ENABLED \
    --action-after-completion DELETE >/dev/null
fi

SCHEDULE=$(aws scheduler get-schedule --profile "$PROFILE" --region "$REGION" \
  --name "$SCHEDULE_NAME" --output json)
if [ "$(jq -r '.State' <<<"$SCHEDULE")" != "ENABLED" ] \
  || [ "$(jq -r '.ScheduleExpression' <<<"$SCHEDULE")" != "$TEARDOWN_EXPRESSION" ] \
  || [ "$(jq -r '.ScheduleExpressionTimezone' <<<"$SCHEDULE")" != "America/Los_Angeles" ]; then
  echo "STOP: teardown schedule readback did not match intent" >&2
  exit 1
fi

echo "Gate 5 guardrails verified: budget_usd=50 alerts=4 teardown=${TEARDOWN_DATE}"
