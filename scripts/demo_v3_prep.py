"""Film-pure prep for Demo v3 on the isolated judge lane.

Idempotent, repeatable. Prepares ONLY representative pre-invoice memory + the
applicable tariff, so the deployed runtime creates every result after intake.

Hero (INV-1048, complete-memory):
  - complete-memory shipment memory for TLLU4829317: boundary events only
    (availability / free-time / gate-out), NO per-day terminal-access heartbeat.
    Any pre-existing TERMINAL_ACCESS_SNAPSHOT rows from the old 6/7 fixture are
    removed so reconstruction resolves 7/7 automatically.
  - the applicable $250/day tariff clause is expected to be seeded by the vector
    seed (gate7); this script does NOT seed the invoice, claims, reconstruction,
    recommendation, approval, or correspondence.

Refusal (INV-1047, MSCU7011453): complete operational history seeded, but NO
matching tariff clause — so the evaluator genuinely returns NEEDS EVIDENCE /
"Governing tariff not verified".

Usage (server-side creds):
  TALLY_CRDB_DSN=... TALLY_TENANT_ID=df78a129-... \
    python -m scripts.demo_v3_prep [--reseed-hero-memory]
"""

from __future__ import annotations

import argparse
import os
import time
import uuid

import psycopg
from psycopg.errors import SerializationFailure

from scripts.demo_v3_approval import TOTAL_MINOR as INV_1041_TOTAL_MINOR
from scripts.demo_v3_approval import drive_inv_1041_approval
from scripts.demo_v3_refusal import TOTAL_MINOR as INV_1047_TOTAL_MINOR
from scripts.demo_v3_refusal import drive_inv_1047_refusal
from src.external.dal import DAL, Tenant
from src.external.reconstruction_seed import seed_reconstruction_memory


def _retry_serializable(fn, attempts: int = 12):
    """Run fn(), retrying on RETRY_SERIALIZABLE. The deployed intake worker loop
    contends on the shared tenant, so a live seal can need a few attempts."""
    for i in range(1, attempts + 1):
        try:
            return fn()
        except SerializationFailure:
            if i == attempts:
                raise
            time.sleep(0.3 * i)


# Deterministic placeholder invoice id for the hero memory seed (artifacts are
# resolved by (tenant_id, public_ref), not invoice_id — this id just satisfies
# the column type and keeps the seed idempotent).
_NS = uuid.UUID("00000000-0000-4000-8000-000000000000")
HERO_SEED_INVOICE = str(uuid.uuid5(_NS, "demo-v3-hero-memory"))

HERO_FIXTURE = None  # default fixture = complete-memory hero
HERO_SHIPMENT = "TLLU4829317"


def _remove_access_heartbeat(cur, tenant_id: str, shipment_ref: str) -> int:
    """Drop any per-day terminal-access snapshots from the hero shipment so the
    complete-memory reconstruction resolves from boundary events alone."""
    cur.execute(
        """
        DELETE FROM shipment_event_memory
        WHERE tenant_id=%s AND shipment_ref=%s
          AND event_type='TERMINAL_ACCESS_SNAPSHOT';
        """,
        (tenant_id, shipment_ref),
    )
    return cur.rowcount




def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", default=os.environ.get("TALLY_TENANT_ID"))
    ap.add_argument("--invoice-hero", default=HERO_SEED_INVOICE)
    args = ap.parse_args()
    tenant_id = args.tenant
    if not tenant_id:
        raise SystemExit("TALLY_TENANT_ID (or --tenant) required")

    dsn = os.environ["TALLY_CRDB_DSN"]
    conn = psycopg.connect(dsn, connect_timeout=20, autocommit=True)
    dal = DAL(conn, Tenant(tenant_id, "demo-v3-prep"))

    with conn.cursor() as cur:
        removed = _remove_access_heartbeat(cur, tenant_id, HERO_SHIPMENT)
    print(f"hero: removed {removed} terminal-access snapshot(s) (complete-memory)")

    # Seed hero complete-memory (boundary events + 2 artifacts). ON CONFLICT
    # DO NOTHING makes this idempotent; the access rows are already gone above.
    hero_counts = seed_reconstruction_memory(dal, invoice_id=args.invoice_hero)
    print(f"hero memory seeded: {hero_counts}")

    # Read-back: assert the hero has NO access heartbeat.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM shipment_event_memory
            WHERE tenant_id=%s AND shipment_ref=%s
              AND event_type='TERMINAL_ACCESS_SNAPSHOT';
            """,
            (tenant_id, HERO_SHIPMENT),
        )
        hero_access = cur.fetchone()[0]
    assert hero_access == 0, f"hero still has {hero_access} access snapshots"
    print(f"read-back OK: hero access snapshots={hero_access}")

    # INV-1047: seed the genuine NEEDS EVIDENCE refusal directly. NOT via the live
    # workers — the deployed FIND_APPLICABLE_RULE worker is hero-hardwired ($250/
    # USOAK/DRY) and would wrongly APPROVE INV-1047. The seed drives the REAL engine
    # to REQUEST_EVIDENCE (no verified rule) → NEEDS_EVIDENCE / "Governing tariff
    # not verified", same posture as the INV-1041 seal. Retry on serialization.
    refusal = _retry_serializable(lambda: drive_inv_1047_refusal(conn, tenant_id))
    print(f"INV-1047 refusal seeded (read-back OK): {refusal}")
    assert refusal["claimed_amount_minor"] == INV_1047_TOTAL_MINOR

    # INV-1041: drive the clean invoice all the way to a genuine sealed historical
    # approval via the REAL Gate-5 approve+seal path, then read back live state.
    approval = _retry_serializable(lambda: drive_inv_1041_approval(conn, tenant_id))
    print(f"INV-1041 approved+sealed (read-back OK): {approval}")
    assert approval["supported_amount_minor"] == INV_1041_TOTAL_MINOR

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
