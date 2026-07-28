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

import psycopg

from src.external.dal import DAL, Tenant
from src.external.reconstruction_seed import (
    INCOMPLETE_MEMORY_FIXTURE,  # noqa: F401 (documents the isolated proof)
    load_source_package,
    seed_reconstruction_memory,
)

HERO_FIXTURE = None  # default fixture = complete-memory hero
REFUSAL_FIXTURE = (
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    + "/tests/fixtures/demo/INV-1047.reconstruction-events.json"
)
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
    ap.add_argument("--invoice-hero", default="demo-v3-hero-memory")
    ap.add_argument("--invoice-refusal", default="demo-v3-refusal-memory")
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

    # Seed the INV-1047 refusal shipment (complete history, distinct shipment).
    refusal_pkg = load_source_package(REFUSAL_FIXTURE)
    refusal_counts = seed_reconstruction_memory(
        dal, invoice_id=args.invoice_refusal, package=refusal_pkg
    )
    print(f"refusal memory seeded: {refusal_counts}")

    # Read-back: assert the hero has NO access heartbeat and the refusal shipment
    # is present.
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
        cur.execute(
            "SELECT count(*) FROM shipment_event_memory "
            "WHERE tenant_id=%s AND shipment_ref=%s;",
            (tenant_id, refusal_pkg["shipment_ref"]),
        )
        refusal_rows = cur.fetchone()[0]
    assert hero_access == 0, f"hero still has {hero_access} access snapshots"
    assert refusal_rows > 0, "refusal shipment memory missing"
    print(f"read-back OK: hero access snapshots={hero_access}, "
          f"refusal rows={refusal_rows}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
