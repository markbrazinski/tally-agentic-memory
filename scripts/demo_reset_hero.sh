#!/usr/bin/env bash
# Reset the hero INV-1048 back to BRAND-NEW (pre-reconstruction) so the deployed
# workers re-run the whole pipeline on the next open, then read back its state.
# Reads TALLY_CRDB_DSN + tenant from SSM via the gate5-deployer profile (same
# wiring as prep_demo_v3_rows.sh). Keeps the PDF + extracted claims; clears only
# the derived reconstruction/decision/send state.
#
#   scripts/demo_reset_hero.sh
set -euo pipefail

PROFILE="${AWS_PROFILE:-gate5-deployer}"
REGION="${AWS_REGION:-us-east-1}"
TENANT="${TALLY_TENANT_ID:-df78a129-6b45-4947-8ce1-5d4352fbd849}"
PY=.venv/bin/python

get() { aws ssm get-parameter --name "$1" --with-decryption --profile "$PROFILE" \
          --region "$REGION" --query 'Parameter.Value' --output text; }

export TALLY_CRDB_DSN="$(get /tally/intake-v1/crdb-dsn)"
export TALLY_TENANT_ID="$TENANT"

echo "== reset hero INV-1048 to fresh, then read back =="
exec $PY -m scripts.demo_reset_hero
