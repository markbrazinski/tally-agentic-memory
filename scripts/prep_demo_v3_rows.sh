#!/usr/bin/env bash
# One command to seed the two pre-existing queue rows on the isolated judge lane,
# then read back live state. Mirrors run_acceptance.sh's SSM/profile wiring.
#
#   scripts/prep_demo_v3_rows.sh
#
# Both rows are seeded DIRECTLY through the real engine (not the deployed workers,
# which are hero-hardwired to $250/USOAK/DRY):
# - INV-1047: engine → REQUEST_EVIDENCE (no verified rule) → NEEDS_EVIDENCE ($875),
#   reason "Governing tariff not verified". A refusal authorizes no action; no seal.
# - INV-1041: engine → APPROVE_FOR_PAYMENT (0 discrepancy) → real Gate-5
#   approve_and_seal → APPROVED FOR PAYMENT ($540), a genuine sealed historical
#   approval "done by a person before".
# demo_v3_prep ends by asserting the live projection == intent for both.
#
# NOTE: the seals contend with the deployed intake worker loop. If a run fails
# with RETRY_SERIALIZABLE even after the built-in retries, briefly set
# TALLY_INTAKE_WORKER_ENABLED=false on the App Runner service, re-run, then
# re-enable it.
set -euo pipefail

PROFILE="${AWS_PROFILE:-gate5-deployer}"
REGION="${AWS_REGION:-us-east-1}"
TENANT="${TALLY_TENANT_ID:-df78a129-6b45-4947-8ce1-5d4352fbd849}"
PY=.venv/bin/python

get() { aws ssm get-parameter --name "$1" --with-decryption --profile "$PROFILE" \
          --region "$REGION" --query 'Parameter.Value' --output text; }

export TALLY_CRDB_DSN="$(get /tally/intake-v1/crdb-dsn)"
export TALLY_TENANT_ID="$TENANT"

echo "== seed INV-1047 refusal + INV-1041 sealed approval, then read back =="
exec $PY -m scripts.demo_v3_prep
