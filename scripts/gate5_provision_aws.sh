#!/usr/bin/env bash
# Provision the bounded Gate 5 AWS runtime foundation. Secrets are accepted
# only through the process environment and written directly to SSM
# SecureString parameters; their values are never echoed.
set -euo pipefail
umask 077

REGION="${AWS_REGION:-us-east-1}"
PROFILE="${AWS_PROFILE:?set AWS_PROFILE to the scoped Gate 5 deployer profile}"
SERVICE_NAME="${TALLY_GATE5_SERVICE_NAME:-tally-gate5-demo}"
ECR_REPO_NAME="${TALLY_GATE5_ECR_REPOSITORY:-tally-gate5-demo}"
ACCESS_ROLE_NAME="${TALLY_GATE5_ACCESS_ROLE_NAME:-tally-gate5-apprunner-ecr}"
INSTANCE_ROLE_NAME="${TALLY_GATE5_INSTANCE_ROLE_NAME:-tally-gate5-apprunner-runtime}"
PARAM_PREFIX="/tally/gate5"
OAUTH_PARAMETER="${PARAM_PREFIX}/oauth-token-bundle"
LEASE_TABLE_NAME="${TALLY_OAUTH_REFRESH_LEASE_TABLE:-tally-gate5-oauth-refresh-lease}"
EVIDENCE_ARNS_FILE="${TALLY_GATE5_EVIDENCE_ARNS_FILE:-runtime-artifacts/gate-5/evidence-object-arns.private.json}"

required=(
  TALLY_CRDB_DSN TALLY_TENANT_ID TALLY_PUBLIC_DEMO_CASE_ID
  TALLY_PUBLIC_DEMO_CONTEST_ID TALLY_MCP_CLUSTER_ID TALLY_MCP_DATABASE
)
for name in "${required[@]}"; do
  if [ -z "${!name:-}" ]; then
    echo "STOP: required private environment value is missing: ${name}" >&2
    exit 1
  fi
done

if [ -z "${TALLY_EVIDENCE_OBJECT_ARNS_JSON:-}" ]; then
  if [ -L "$EVIDENCE_ARNS_FILE" ] || [ ! -f "$EVIDENCE_ARNS_FILE" ]; then
    echo "STOP: private exact evidence-ARN input is missing" >&2
    exit 1
  fi
  TALLY_EVIDENCE_OBJECT_ARNS_JSON=$(jq -c '.object_arns' "$EVIDENCE_ARNS_FILE")
fi

if ! jq -e 'type == "array" and length > 0 and all(.[]; type == "string" and startswith("arn:aws:s3:::"))' \
  >/dev/null <<<"${TALLY_EVIDENCE_OBJECT_ARNS_JSON}"; then
  echo "STOP: TALLY_EVIDENCE_OBJECT_ARNS_JSON must be a non-empty JSON array of S3 object ARNs" >&2
  exit 1
fi

ACCOUNT_ID=$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text)
if [[ ! "$ACCOUNT_ID" =~ ^[0-9]{12}$ ]]; then
  echo "STOP: scoped AWS identity did not return a valid account" >&2
  exit 1
fi

TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

put_secure_parameter() {
  local name="$1"
  local value="$2"
  local request_file="$TMP_DIR/parameter.json"
  printf '%s' "$value" | jq -Rs --arg name "$name" \
    '{Name:$name,Value:.,Type:"SecureString",Overwrite:true}' >"$request_file"
  aws ssm put-parameter --profile "$PROFILE" --region "$REGION" \
    --cli-input-json "file://${request_file}" >/dev/null
  local live_type
  live_type=$(aws ssm get-parameter --profile "$PROFILE" --region "$REGION" \
    --name "$name" --no-with-decryption --query 'Parameter.Type' --output text)
  if [ "$live_type" != "SecureString" ]; then
    echo "STOP: parameter readback was not SecureString" >&2
    exit 1
  fi
}

for mapping in \
  "crdb-dsn:TALLY_CRDB_DSN" \
  "tenant-id:TALLY_TENANT_ID" \
  "hero-case-id:TALLY_PUBLIC_DEMO_CASE_ID" \
  "hero-contest-id:TALLY_PUBLIC_DEMO_CONTEST_ID" \
  "mcp-cluster-id:TALLY_MCP_CLUSTER_ID" \
  "mcp-database:TALLY_MCP_DATABASE"; do
  parameter="${mapping%%:*}"
  variable="${mapping##*:}"
  put_secure_parameter "${PARAM_PREFIX}/${parameter}" "${!variable}"
done

OAUTH_TYPE=$(aws ssm get-parameter --profile "$PROFILE" --region "$REGION" \
  --name "$OAUTH_PARAMETER" --no-with-decryption --query 'Parameter.Type' --output text)
if [ "$OAUTH_TYPE" != "SecureString" ]; then
  echo "STOP: proven OAuth token bundle is absent or not a SecureString" >&2
  exit 1
fi
if aws ssm get-parameter --profile "$PROFILE" --region "$REGION" \
  --name "${PARAM_PREFIX}/mcp-api-key" --no-with-decryption >/dev/null 2>&1; then
  echo "STOP: prohibited MCP API-key fallback parameter still exists" >&2
  exit 1
fi

cat >"$TMP_DIR/ecr-trust.json" <<'JSON'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"build.apprunner.amazonaws.com"},"Action":"sts:AssumeRole"}]}
JSON
cat >"$TMP_DIR/runtime-trust.json" <<'JSON'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"tasks.apprunner.amazonaws.com"},"Action":"sts:AssumeRole"}]}
JSON

ensure_role() {
  local role_name="$1"
  local trust_file="$2"
  if ! aws iam get-role --profile "$PROFILE" --role-name "$role_name" >/dev/null 2>&1; then
    aws iam create-role --profile "$PROFILE" --role-name "$role_name" \
      --assume-role-policy-document "file://${trust_file}" \
      --tags Key=Project,Value=Tally Key=Environment,Value=synthetic-gate5 \
        Key=TeardownDate,Value=2026-09-30 >/dev/null
  fi
}

ensure_role "$ACCESS_ROLE_NAME" "$TMP_DIR/ecr-trust.json"
ensure_role "$INSTANCE_ROLE_NAME" "$TMP_DIR/runtime-trust.json"

cat >"$TMP_DIR/ecr-policy.json" <<JSON
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":["ecr:GetAuthorizationToken"],"Resource":"*"},
 {"Effect":"Allow","Action":["ecr:BatchCheckLayerAvailability","ecr:GetDownloadUrlForLayer","ecr:BatchGetImage"],"Resource":"arn:aws:ecr:${REGION}:${ACCOUNT_ID}:repository/${ECR_REPO_NAME}"}
]}
JSON

jq -n \
  --arg region "$REGION" \
  --arg account "$ACCOUNT_ID" \
  --arg prefix "$PARAM_PREFIX" \
  --arg oauth "$OAUTH_PARAMETER" \
  --arg lease "$LEASE_TABLE_NAME" \
  --argjson evidence "$TALLY_EVIDENCE_OBJECT_ARNS_JSON" \
  '{Version:"2012-10-17",Statement:[
    {Sid:"ReadFixedGate5Configuration",Effect:"Allow",Action:["ssm:GetParameter","ssm:GetParameters"],Resource:[
      ("arn:aws:ssm:"+$region+":"+$account+":parameter"+$prefix+"/crdb-dsn"),
      ("arn:aws:ssm:"+$region+":"+$account+":parameter"+$prefix+"/tenant-id"),
      ("arn:aws:ssm:"+$region+":"+$account+":parameter"+$prefix+"/hero-case-id"),
      ("arn:aws:ssm:"+$region+":"+$account+":parameter"+$prefix+"/hero-contest-id"),
      ("arn:aws:ssm:"+$region+":"+$account+":parameter"+$prefix+"/mcp-cluster-id"),
      ("arn:aws:ssm:"+$region+":"+$account+":parameter"+$prefix+"/mcp-database")
    ]},
    {Sid:"RotateOneOAuthBundle",Effect:"Allow",Action:["ssm:GetParameter","ssm:PutParameter"],Resource:("arn:aws:ssm:"+$region+":"+$account+":parameter"+$oauth)},
    {Sid:"CoordinateOneOAuthRefresh",Effect:"Allow",Action:["dynamodb:PutItem","dynamodb:DeleteItem"],Resource:("arn:aws:dynamodb:"+$region+":"+$account+":table/"+$lease),Condition:{"ForAllValues:StringEquals":{"dynamodb:LeadingKeys":[$oauth]}}},
    {Sid:"ReadExactVersionedEvidence",Effect:"Allow",Action:["s3:GetObjectVersion"],Resource:$evidence}
  ]}' >"$TMP_DIR/runtime-policy.json"

aws iam put-role-policy --profile "$PROFILE" --role-name "$ACCESS_ROLE_NAME" \
  --policy-name tally-gate5-ecr-pull --policy-document "file://$TMP_DIR/ecr-policy.json" >/dev/null
aws iam put-role-policy --profile "$PROFILE" --role-name "$INSTANCE_ROLE_NAME" \
  --policy-name tally-gate5-runtime-bounded --policy-document "file://$TMP_DIR/runtime-policy.json" >/dev/null

if ! aws ecr describe-repositories --profile "$PROFILE" --region "$REGION" \
  --repository-names "$ECR_REPO_NAME" >/dev/null 2>&1; then
  aws ecr create-repository --profile "$PROFILE" --region "$REGION" \
    --repository-name "$ECR_REPO_NAME" --image-scanning-configuration scanOnPush=true \
    --tags Key=Project,Value=Tally Key=Environment,Value=synthetic-gate5 \
      Key=TeardownDate,Value=2026-09-30 >/dev/null
fi

if ! aws dynamodb describe-table --profile "$PROFILE" --region "$REGION" \
  --table-name "$LEASE_TABLE_NAME" >/dev/null 2>&1; then
  aws dynamodb create-table --profile "$PROFILE" --region "$REGION" \
    --table-name "$LEASE_TABLE_NAME" --billing-mode PAY_PER_REQUEST \
    --attribute-definitions AttributeName=bundle_key,AttributeType=S \
    --key-schema AttributeName=bundle_key,KeyType=HASH \
    --tags Key=Project,Value=Tally Key=Environment,Value=synthetic-gate5 \
      Key=TeardownDate,Value=2026-09-30 >/dev/null
  aws dynamodb wait table-exists --profile "$PROFILE" --region "$REGION" \
    --table-name "$LEASE_TABLE_NAME"
fi
LEASE_ARN=$(aws dynamodb describe-table --profile "$PROFILE" --region "$REGION" \
  --table-name "$LEASE_TABLE_NAME" --query 'Table.TableArn' --output text)
aws dynamodb tag-resource --profile "$PROFILE" --region "$REGION" \
  --resource-arn "$LEASE_ARN" \
  --tags Key=Project,Value=Tally Key=Environment,Value=synthetic-gate5 \
    Key=TeardownDate,Value=2026-09-30 >/dev/null
TTL_STATUS=$(aws dynamodb describe-time-to-live --profile "$PROFILE" --region "$REGION" \
  --table-name "$LEASE_TABLE_NAME" --query 'TimeToLiveDescription.TimeToLiveStatus' \
  --output text)
if [ "$TTL_STATUS" != "ENABLED" ] && [ "$TTL_STATUS" != "ENABLING" ]; then
  aws dynamodb update-time-to-live --profile "$PROFILE" --region "$REGION" \
    --table-name "$LEASE_TABLE_NAME" \
    --time-to-live-specification Enabled=true,AttributeName=expires_at >/dev/null
fi

ROLE_ARN=$(aws iam get-role --profile "$PROFILE" --role-name "$INSTANCE_ROLE_NAME" \
  --query 'Role.Arn' --output text)
LEASE_LIVE=$(aws dynamodb describe-table --profile "$PROFILE" --region "$REGION" \
  --table-name "$LEASE_TABLE_NAME" --output json)
TTL_LIVE=$(aws dynamodb describe-time-to-live --profile "$PROFILE" --region "$REGION" \
  --table-name "$LEASE_TABLE_NAME" --output json)
LEASE_TAGS=$(aws dynamodb list-tags-of-resource --profile "$PROFILE" --region "$REGION" \
  --resource-arn "$LEASE_ARN" --output json)
PARAMETERS=$(aws ssm get-parameters --profile "$PROFILE" --region "$REGION" \
  --names "${PARAM_PREFIX}/crdb-dsn" "${PARAM_PREFIX}/tenant-id" \
    "${PARAM_PREFIX}/hero-case-id" "${PARAM_PREFIX}/hero-contest-id" \
    "${PARAM_PREFIX}/mcp-cluster-id" "${PARAM_PREFIX}/mcp-database" \
    "$OAUTH_PARAMETER" --no-with-decryption --output json)
RUNTIME_POLICY=$(aws iam get-role-policy --profile "$PROFILE" \
  --role-name "$INSTANCE_ROLE_NAME" --policy-name tally-gate5-runtime-bounded \
  --query 'PolicyDocument' --output json)
if ! jq -e '
    (.Parameters | length) == 7 and
    all(.Parameters[]; .Type == "SecureString")
  ' >/dev/null <<<"$PARAMETERS" \
  || ! jq -e '
    .Table.TableStatus == "ACTIVE" and
    .Table.BillingModeSummary.BillingMode == "PAY_PER_REQUEST" and
    .Table.KeySchema == [{"AttributeName":"bundle_key","KeyType":"HASH"}]
  ' >/dev/null <<<"$LEASE_LIVE" \
  || ! jq -e '
    .TimeToLiveDescription.AttributeName == "expires_at" and
    (.TimeToLiveDescription.TimeToLiveStatus == "ENABLED" or
     .TimeToLiveDescription.TimeToLiveStatus == "ENABLING")
  ' >/dev/null <<<"$TTL_LIVE" \
  || ! jq -e '
    any(.Tags[]; .Key == "Project" and .Value == "Tally") and
    any(.Tags[]; .Key == "Environment" and .Value == "synthetic-gate5") and
    any(.Tags[]; .Key == "TeardownDate" and .Value == "2026-09-30")
  ' >/dev/null <<<"$LEASE_TAGS" \
  || ! jq -e '
    [.Statement[] | select(.Sid == "RotateOneOAuthBundle")][0]
      | .Action == ["ssm:GetParameter","ssm:PutParameter"]
  ' >/dev/null <<<"$RUNTIME_POLICY" \
  || ! jq -e --arg oauth "$OAUTH_PARAMETER" '
    [.Statement[] | select(.Sid == "CoordinateOneOAuthRefresh")][0]
      | .Condition["ForAllValues:StringEquals"]["dynamodb:LeadingKeys"] == [$oauth]
  ' >/dev/null <<<"$RUNTIME_POLICY" \
  || [[ ! "$ROLE_ARN" =~ ^arn:aws:iam::[0-9]{12}:role/ ]]; then
  echo "STOP: provisioned state did not match intent" >&2
  exit 1
fi

echo "Gate 5 AWS foundation verified: roles=2 required_secure_parameters=7 ecr_repository=1 oauth_refresh_lease=1"
