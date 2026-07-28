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
import json
import os
import uuid

import psycopg

from scripts.demo_v3_approval import TOTAL_MINOR as INV_1041_TOTAL_MINOR
from scripts.demo_v3_approval import drive_inv_1041_approval
from src.external.dal import DAL, Tenant
from src.external.reconstruction_seed import (
    INCOMPLETE_MEMORY_FIXTURE,  # noqa: F401 (documents the isolated proof)
    load_source_package,
    seed_reconstruction_memory,
)

# Deterministic placeholder invoice ids for the seed rows (the artifacts are
# resolved by (tenant_id, public_ref), not invoice_id — these ids just satisfy
# the column type and keep the seed idempotent).
_NS = uuid.UUID("00000000-0000-4000-8000-000000000000")
HERO_SEED_INVOICE = str(uuid.uuid5(_NS, "demo-v3-hero-memory"))
REFUSAL_SEED_INVOICE = str(uuid.uuid5(_NS, "demo-v3-refusal-memory"))

REFUSAL_TOTAL_MINOR = 87500  # INV-1047: $125/day × 7 = $875 (carrier total)

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


def _readback_refusal_projection(cur, tenant_id: str) -> dict[str, object] | None:
    """Read back the LIVE INV-1047 queue projection once the operator has imported
    its PDF and the workers have resolved. Asserts NEEDS_EVIDENCE / "Governing
    tariff not verified" / $875. Returns None (with a note) if the invoice has not
    been imported yet — the memory this script seeds is a precondition, the
    projection is produced by the deployed runtime after intake."""
    cur.execute("SELECT id, aggregate_status FROM invoices WHERE tenant_id=%s AND "
                "invoice_no='INV-1047';", (tenant_id,))
    inv = cur.fetchone()
    if inv is None:
        return None
    invoice_id, aggregate = str(inv[0]), inv[1]
    cur.execute(
        "SELECT recommendation_type, reason_codes FROM recommendations "
        "WHERE tenant_id=%s AND invoice_id=%s AND superseded_by IS NULL "
        "ORDER BY version DESC LIMIT 1;",
        (tenant_id, invoice_id),
    )
    rec = cur.fetchone()
    cur.execute(
        "SELECT COALESCE(sum(amount_minor),0) FROM extracted_claims c "
        "JOIN claim_sets s ON s.tenant_id=c.tenant_id AND s.id=c.claim_set_id "
        "WHERE c.tenant_id=%s AND s.invoice_id=%s AND c.field_name='total';",
        (tenant_id, invoice_id),
    )
    total = int(cur.fetchone()[0])
    rec_type = rec[0] if rec else None
    reasons = rec[1] if rec else []
    reasons = reasons if isinstance(reasons, list) else json.loads(reasons or "[]")
    result = {"invoice_id": invoice_id, "aggregate_status": aggregate,
              "recommendation_type": rec_type, "reason_codes": reasons,
              "total_minor": total}
    assert aggregate == "NEEDS_EVIDENCE", f"INV-1047 aggregate {aggregate}"
    assert rec_type == "REQUEST_EVIDENCE", f"INV-1047 engine {rec_type}"
    assert "RULE_NOT_VERIFIED" in reasons, f"INV-1047 reasons {reasons}"
    assert total == REFUSAL_TOTAL_MINOR, f"INV-1047 total {total}"
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", default=os.environ.get("TALLY_TENANT_ID"))
    ap.add_argument("--invoice-hero", default=HERO_SEED_INVOICE)
    ap.add_argument("--invoice-refusal", default=REFUSAL_SEED_INVOICE)
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

    # INV-1041: drive the clean invoice all the way to a genuine sealed historical
    # approval via the REAL Gate-5 approve+seal path, then read back live state.
    approval = drive_inv_1041_approval(conn, tenant_id)
    print(f"INV-1041 approved+sealed (read-back OK): {approval}")
    assert approval["supported_amount_minor"] == INV_1041_TOTAL_MINOR

    # INV-1047: best-effort projection read-back. The memory above is the
    # precondition; the NEEDS_EVIDENCE projection is produced by the deployed
    # runtime after the operator imports INV-1047.pdf. Assert it if present.
    with conn.cursor() as cur:
        refusal_projection = _readback_refusal_projection(cur, tenant_id)
    if refusal_projection is None:
        print("INV-1047 projection: invoice not yet imported — run the operator "
              "PDF import, then re-run to assert NEEDS_EVIDENCE / $875.")
    else:
        print(f"INV-1047 projection read-back OK: {refusal_projection}")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
