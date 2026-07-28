#!/usr/bin/env bash
# One command to make the two pre-existing queue rows real on the isolated judge
# lane, then read back live state. Mirrors run_acceptance.sh's SSM/profile wiring.
#
#   scripts/prep_demo_v3_rows.sh
#
# - INV-1047: import its PDF through the deployed intake API + seed its memory,
#   so the deployed workers resolve the genuine NEEDS_EVIDENCE ($875) refusal.
# - INV-1041: drive the real engine + real Gate-5 approve_and_seal so the queue
#   shows a genuine sealed APPROVED FOR PAYMENT ($540) "done by a person before".
# Ends by asserting the live projection == intent (via demo_v3_prep's read-back).
set -euo pipefail

PROFILE="${AWS_PROFILE:-gate5-deployer}"
REGION="${AWS_REGION:-us-east-1}"
TENANT="${TALLY_TENANT_ID:-df78a129-6b45-4947-8ce1-5d4352fbd849}"
PY=.venv/bin/python

get() { aws ssm get-parameter --name "$1" --with-decryption --profile "$PROFILE" \
          --region "$REGION" --query 'Parameter.Value' --output text; }

export TALLY_CRDB_DSN="$(get /tally/intake-v1/crdb-dsn)"
export TALLY_TENANT_ID="$TENANT"
PW="$(get /tally/intake-v1/judge-password)"
USER="$(get /tally/intake-v1/judge-username)"
ARN="$(aws apprunner list-services --profile "$PROFILE" --region "$REGION" \
        --query "ServiceSummaryList[?ServiceName=='tally-intake-v1'].ServiceArn|[0]" --output text)"
URL="https://$(aws apprunner describe-service --profile "$PROFILE" --region "$REGION" \
        --service-arn "$ARN" --query 'Service.ServiceUrl' --output text)"

echo "== login to the deployed intake API =="
COOKIE="$(mktemp)"
curl -s -c "$COOKIE" -X POST "$URL/api/login" -H 'content-type: application/json' \
  -d "{\"username\":\"$USER\",\"password\":\"$PW\"}" -o /dev/null

echo "== import INV-1047.pdf (real intake → genuine NEEDS EVIDENCE refusal) =="
INV1047="$(curl -s -b "$COOKIE" -X POST "$URL/api/demo/invoices" \
        -H "Idempotency-Key: prep-1047-$(date +%s)-$RANDOM" \
        -F "file=@tests/fixtures/demo/INV-1047.pdf;type=application/pdf" \
        -F "demo_scenario=locked-inv-1047" -F "import_source=operator_import" \
      | $PY -c "import sys,json;print((json.load(sys.stdin).get('invoice') or {}).get('invoice_id',''))" || true)"
rm -f "$COOKIE"
# NOTE: if this prints empty, INV-1047 needs adding to ALLOWED_SCENARIOS in
# intake_api.py + a redeploy (see the hand-off notes). The seal-drive below is
# independent and still runs.
echo "   INV-1047 invoice_id=${INV1047:-<pending — see notes>}"

echo "== drive INV-1041 through real engine + real Gate-5 seal, + read back both =="
exec $PY -m scripts.demo_v3_prep
