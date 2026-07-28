#!/usr/bin/env bash
# One-command deployed authority-transition acceptance on the isolated judge lane.
# Preps a fresh invoice (import through the real API + seed its source artifacts +
# reset June-11 to PENDING), then runs the 13-check harness. Repeatable.
#
#   scripts/run_acceptance.sh            # run once
#   RUN=2 scripts/run_acceptance.sh      # label it run 2
set -euo pipefail

PROFILE="${AWS_PROFILE:-gate5-deployer}"
REGION="${AWS_REGION:-us-east-1}"
TENANT="${TALLY_TENANT_ID:-df78a129-6b45-4947-8ce1-5d4352fbd849}"
RUN="${RUN:-1}"
PY=.venv/bin/python

get() { aws ssm get-parameter --name "$1" --with-decryption --profile "$PROFILE" \
          --region "$REGION" --query 'Parameter.Value' --output text; }

export TALLY_CRDB_DSN="$(get /tally/intake-v1/crdb-dsn)"
PW="$(get /tally/intake-v1/judge-password)"
USER="$(aws ssm get-parameter --name /tally/intake-v1/judge-username --profile "$PROFILE" \
          --region "$REGION" --query 'Parameter.Value' --output text)"
ARN="$(aws apprunner list-services --profile "$PROFILE" --region "$REGION" \
        --query "ServiceSummaryList[?ServiceName=='tally-intake-v1'].ServiceArn|[0]" --output text)"
URL="https://$(aws apprunner describe-service --profile "$PROFILE" --region "$REGION" \
        --service-arn "$ARN" --query 'Service.ServiceUrl' --output text)"

echo "== clean slate: remove any prior INV-1048 test invoice, keep retained memory =="
$PY scripts/_acceptance_reset.py

echo "== import a fresh invoice through the deployed intake API =="
COOKIE="$(mktemp)"
curl -s -c "$COOKIE" -X POST "$URL/api/login" -H 'content-type: application/json' \
  -d "{\"username\":\"$USER\",\"password\":\"$PW\"}" -o /dev/null
INV="$(curl -s -b "$COOKIE" -X POST "$URL/api/demo/invoices" \
        -H "Idempotency-Key: acceptance-$(date +%s)-$RANDOM" \
        -F "file=@tests/fixtures/demo/INV-1048.pdf;type=application/pdf" \
        -F "demo_scenario=locked-inv-1048" -F "import_source=operator_import" \
      | $PY -c "import sys,json;print((json.load(sys.stdin).get('invoice') or {}).get('invoice_id',''))")"
rm -f "$COOKIE"
[ -n "$INV" ] || { echo "import failed"; exit 1; }
echo "   invoice_id=$INV"

echo "== seed retained source artifacts for this invoice =="
$PY -c "
import os, psycopg
from src.external.dal import DAL, Tenant
from src.external.reconstruction_seed import seed_reconstruction_memory
c=psycopg.connect(os.environ['TALLY_CRDB_DSN'], connect_timeout=20, autocommit=True)
print('  ', seed_reconstruction_memory(DAL(c, Tenant('$TENANT','ui-seed')), invoice_id='$INV'))
"

echo "== run the 13-check acceptance harness =="
exec $PY scripts/authority_acceptance.py --url "$URL" --username "$USER" \
  --password "$PW" --pdf tests/fixtures/demo/INV-1048.pdf --run "$RUN"
