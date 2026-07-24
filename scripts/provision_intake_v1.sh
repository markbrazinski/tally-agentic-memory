#!/usr/bin/env bash
# Provision only the isolated Intake v1 runtime. Existing Gate 5 resources are untouched.
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
PROFILE="${AWS_PROFILE:-gate5-deployer}"
ACCOUNT_ID="$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text)"
PREFIX="/tally/intake-v1"
BUCKET="${TALLY_INTAKE_BUCKET:-tally-record}"
KEY_PREFIX="${TALLY_INTAKE_KEY_PREFIX:-isolated-intake-v1}"
RUNTIME_ROLE="tally-intake-v1-runtime"
ACCESS_ROLE="tally-intake-v1-ecr-access"
REPOSITORY="tally-intake-v1"

: "${TALLY_INTAKE_ISOLATED_DSN:?set the isolated CockroachDB DSN}"
: "${TALLY_INTAKE_TENANT_ID:?set the isolated synthetic tenant UUID}"
: "${TALLY_INTAKE_DEMO_TOKEN:?set a generated private uploader token}"

aws ssm put-parameter --profile "$PROFILE" --region "$REGION" \
  --name "${PREFIX}/crdb-dsn" --type SecureString \
  --value "$TALLY_INTAKE_ISOLATED_DSN" --overwrite >/dev/null
aws ssm put-parameter --profile "$PROFILE" --region "$REGION" \
  --name "${PREFIX}/tenant-id" --type SecureString \
  --value "$TALLY_INTAKE_TENANT_ID" --overwrite >/dev/null
aws ssm put-parameter --profile "$PROFILE" --region "$REGION" \
  --name "${PREFIX}/demo-token" --type SecureString \
  --value "$TALLY_INTAKE_DEMO_TOKEN" --overwrite >/dev/null

TRUST_POLICY="$(jq -n '{
  Version:"2012-10-17",
  Statement:[{
    Effect:"Allow",
    Principal:{Service:"tasks.apprunner.amazonaws.com"},
    Action:"sts:AssumeRole"
  }]
}')"
if ! aws iam get-role --profile "$PROFILE" --role-name "$RUNTIME_ROLE" >/dev/null 2>&1; then
  aws iam create-role --profile "$PROFILE" --role-name "$RUNTIME_ROLE" \
    --assume-role-policy-document "$TRUST_POLICY" \
    --tags Key=Project,Value=Tally Key=Environment,Value=intake-v1-isolated >/dev/null
fi
RUNTIME_POLICY="$(jq -n \
  --arg region "$REGION" --arg account "$ACCOUNT_ID" \
  --arg bucket "$BUCKET" --arg key_prefix "$KEY_PREFIX" --arg prefix "$PREFIX" '{
  Version:"2012-10-17",
  Statement:[
    {
      Sid:"ExactVersionedInvoiceObjects",
      Effect:"Allow",
      Action:["s3:PutObject","s3:GetObject","s3:GetObjectVersion"],
      Resource:[("arn:aws:s3:::"+$bucket+"/"+$key_prefix+"/*")]
    },
    {
      Sid:"RecoverUploadedVersionByChecksum",
      Effect:"Allow",
      Action:["s3:ListBucketVersions"],
      Resource:[("arn:aws:s3:::"+$bucket)],
      Condition:{StringLike:{"s3:prefix":[($key_prefix+"/*")]}}
    },
    {
      Sid:"RealIntakeBedrockExtraction",
      Effect:"Allow",
      Action:["bedrock:InvokeModel"],
      Resource:[
        ("arn:aws:bedrock:"+$region+":"+$account+
         ":inference-profile/us.anthropic.claude-sonnet-4-6"),
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-4-6*"
      ]
    },
    {
      Sid:"IsolatedIntakeSecrets",
      Effect:"Allow",
      Action:["ssm:GetParameter","ssm:GetParameters"],
      Resource:[("arn:aws:ssm:"+$region+":"+$account+":parameter"+$prefix+"/*")]
    }
  ]
}')"
aws iam put-role-policy --profile "$PROFILE" --role-name "$RUNTIME_ROLE" \
  --policy-name "tally-intake-v1-runtime" \
  --policy-document "$RUNTIME_POLICY"

ECR_TRUST="$(jq -n '{
  Version:"2012-10-17",
  Statement:[{
    Effect:"Allow",
    Principal:{Service:"build.apprunner.amazonaws.com"},
    Action:"sts:AssumeRole"
  }]
}')"
if ! aws iam get-role --profile "$PROFILE" --role-name "$ACCESS_ROLE" >/dev/null 2>&1; then
  aws iam create-role --profile "$PROFILE" --role-name "$ACCESS_ROLE" \
    --assume-role-policy-document "$ECR_TRUST" \
    --tags Key=Project,Value=Tally Key=Environment,Value=intake-v1-isolated >/dev/null
fi
ECR_POLICY="$(jq -n --arg account "$ACCOUNT_ID" --arg region "$REGION" '{
  Version:"2012-10-17",
  Statement:[{
    Effect:"Allow",
    Action:[
      "ecr:GetAuthorizationToken",
      "ecr:BatchCheckLayerAvailability",
      "ecr:GetDownloadUrlForLayer",
      "ecr:BatchGetImage",
      "ecr:DescribeImages"
    ],
    Resource:"*"
  }]
}')"
aws iam put-role-policy --profile "$PROFILE" --role-name "$ACCESS_ROLE" \
  --policy-name "tally-intake-v1-ecr-read" --policy-document "$ECR_POLICY"

if ! aws ecr describe-repositories --profile "$PROFILE" --region "$REGION" \
  --repository-names "$REPOSITORY" >/dev/null 2>&1; then
  aws ecr create-repository --profile "$PROFILE" --region "$REGION" \
    --repository-name "$REPOSITORY" \
    --image-scanning-configuration scanOnPush=true \
    --tags Key=Project,Value=Tally Key=Environment,Value=intake-v1-isolated >/dev/null
fi

echo "isolated Intake v1 configuration provisioned"
