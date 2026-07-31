"""Reset + FRESH-IMPORT the hero (INV-1048) through the real deployed pipeline.

Unlike ``demo_restore_hero`` (which seeds the reconstruction/decision chain
directly), this drives the REAL path end to end: the PDF enters through the
authenticated multipart intake endpoint and every worker runs for itself --
Bedrock claim extraction, Managed-MCP reconstruction, vector applicable-rule
retrieval, and deterministic judgment. Nothing downstream is seeded.

Ordering (this is the whole trick):

  1. delete the existing hero so its sha256 is free -- otherwise
     find_invoice_by_sha dedups the import to the existing invoice and NO worker
     runs (intake_repository.find_invoice_by_sha / complete_duplicate_ingestion).
  2. POST the PDF through the real endpoint and get the new invoice_id.
  3. seed the per-invoice source artifacts IMMEDIATELY. They cannot be seeded
     earlier: reconstruction_source_artifacts.invoice_id is NOT NULL with an FK
     to invoices, so the artifacts cannot exist before the invoice they belong
     to. Reconstruction needs them, and START_RECONSTRUCTION is enqueued as soon
     as claim validation finishes -- so this must land inside that window.
  4. poll until the pipeline settles, then print the invariants.

The pre-invoice shipment memory in ``shipment_event_memory`` (what the MCP view
reads) is NOT touched -- it is standing memory that predates any invoice, and
re-seeding it is a no-op upsert on public_ref.

Build-time demo housekeeping only (isolated judge lane).

Usage:
  AWS_PROFILE=gate5-deployer python -m scripts.demo_fresh_hero
  AWS_PROFILE=gate5-deployer python -m scripts.demo_fresh_hero --keep  # no delete
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from pathlib import Path

import psycopg

REPO_ROOT = Path(__file__).resolve().parents[1]
HERO_PDF = REPO_ROOT / "tests" / "fixtures" / "demo" / "INV-1048.pdf"
HERO_DISPLAY_NAME = "INV-1048.pdf"
DEFAULT_URL = "https://r3n3ixixr3.us-east-1.awsapprunner.com"

# Expected end state — the locked demo story. Printed as PASS/FAIL, never forced.
EXPECT_CLAIMED_MINOR = 245000
EXPECT_SUPPORTED_MINOR = 175000
EXPECT_DISPUTED_MINOR = 70000
EXPECT_DAYS = 7
EXPECT_DELTA_PER_DAY_MINOR = 10000

SETTLED_STATUSES = {"READY_FOR_REVIEW", "DISPUTED", "APPROVED_FOR_PAYMENT",
                    "NEEDS_EVIDENCE"}


def _ssm(name: str) -> str:
    import boto3

    client = boto3.client("ssm", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    return client.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]


def _connect() -> psycopg.Connection:
    dsn = os.environ.get("TALLY_CRDB_DSN") or _ssm("/tally/intake-v1/crdb-dsn")
    return psycopg.connect(dsn, connect_timeout=20, autocommit=True)


def _tenant() -> str:
    return os.environ.get("TALLY_TENANT_ID") or _ssm("/tally/intake-v1/tenant-id")


def _token() -> str:
    return os.environ.get("TALLY_DEMO_TOKEN") or _ssm("/tally/intake-v1/demo-token")


def _elapsed(started: float) -> str:
    return f"{time.monotonic() - started:6.1f}s"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default=os.environ.get("TALLY_DEMO_URL", DEFAULT_URL))
    ap.add_argument("--keep", action="store_true",
                    help="do not delete the existing hero first (debugging)")
    ap.add_argument("--timeout", type=float, default=300.0,
                    help="seconds to wait for the pipeline to settle")
    args = ap.parse_args()

    if not HERO_PDF.exists():
        print(f"STOP: hero fixture missing: {HERO_PDF}", file=sys.stderr)
        return 2

    tenant_id = _tenant()
    started = time.monotonic()

    # 1. Free the sha256 so the import is a real import, not a dedup replay.
    if not args.keep:
        from scripts._demo_delete_invoice import delete_invoice

        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM invoices WHERE tenant_id=%s AND display_name=%s;",
                (tenant_id, HERO_DISPLAY_NAME),
            )
            row = cur.fetchone()
            if row is None:
                print(f"[{_elapsed(started)}] no existing {HERO_DISPLAY_NAME} to clear")
            else:
                delete_invoice(cur, tenant_id, str(row[0]))
                print(f"[{_elapsed(started)}] cleared existing hero {row[0]}")

    # 2. Real authenticated multipart import.
    from scripts.operator_import import import_pdf

    result = import_pdf(
        base_url=args.url,
        pdf_path=str(HERO_PDF),
        source="operator_import",
        idempotency_key=f"fresh-hero-{uuid.uuid4()}",
        bearer=_token(),
    )
    invoice = result["snapshot"]["invoice"]
    invoice_id = invoice["invoice_id"]
    if result["replay"]:
        print(f"[{_elapsed(started)}] STOP: import was a DEDUP REPLAY (HTTP 200) — "
              "the previous invoice still holds this sha256, so no worker will run.",
              file=sys.stderr)
        return 3
    print(f"[{_elapsed(started)}] imported {invoice_id} (HTTP {result['status']})")

    # 3. Seed the per-invoice source artifacts before reconstruction claims the task.
    from src.external.dal import DAL, Tenant
    from src.external.reconstruction_seed import seed_reconstruction_memory

    with _connect() as conn:
        dal = DAL(conn, Tenant(tenant_id, "demo-fresh-hero"))
        counts = seed_reconstruction_memory(dal, invoice_id=invoice_id)
    print(f"[{_elapsed(started)}] seeded source artifacts + memory: {counts}")

    # 4. Wait for the pipeline to settle, logging each worker as it completes.
    seen: dict[str, str] = {}
    status = None
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT status FROM invoices WHERE tenant_id=%s AND id=%s;",
                        (tenant_id, invoice_id))
            row = cur.fetchone()
            status = row[0] if row else None
            cur.execute(
                "SELECT task_type, state FROM workflow_tasks "
                "WHERE tenant_id=%s AND invoice_id=%s ORDER BY created_at;",
                (tenant_id, invoice_id),
            )
            tasks = cur.fetchall()
        for task_type, state in tasks:
            if seen.get(task_type) != state:
                seen[task_type] = state
                print(f"[{_elapsed(started)}] {task_type:<24} {state}")
        if status in SETTLED_STATUSES and all(
            s in ("COMPLETED", "FAILED") for _, s in tasks
        ):
            break
        time.sleep(3)

    return _report(tenant_id, invoice_id, status, started)


def _report(tenant_id: str, invoice_id: str, status: str | None,
            started: float) -> int:
    """Print the pre-recording invariants. Reports actual values, never forces."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT w.task_type, a.attempt, a.state, a.private_error_code "
            "FROM workflow_task_attempts a JOIN workflow_tasks w "
            "  ON w.id=a.task_id AND w.tenant_id=a.tenant_id "
            "WHERE w.tenant_id=%s AND w.invoice_id=%s ORDER BY a.started_at;",
            (tenant_id, invoice_id),
        )
        attempts = cur.fetchall()
        cur.execute(
            "SELECT count(*), count(*) FILTER (WHERE recorded_before_cutoff) "
            "FROM reconstruction_events WHERE tenant_id=%s AND invoice_id=%s;",
            (tenant_id, invoice_id),
        )
        n_events, n_before = cur.fetchone()
        cur.execute(
            "SELECT count(*) FROM reconstruction_day_event_bindings WHERE tenant_id=%s "
            "AND charged_day_id IN (SELECT id FROM reconstruction_charged_days "
            "WHERE tenant_id=%s AND invoice_id=%s);",
            (tenant_id, tenant_id, invoice_id),
        )
        n_bindings = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*), coalesce(sum(dispute_amount_minor),0) "
            "FROM reconstruction_charged_days WHERE tenant_id=%s AND invoice_id=%s;",
            (tenant_id, invoice_id),
        )
        n_days, sum_delta = cur.fetchone()
        cur.execute(
            "SELECT version, recommendation_type, state, claimed_amount_minor, "
            "supported_amount_minor, disputed_amount_minor "
            "FROM recommendations WHERE tenant_id=%s AND invoice_id=%s "
            "AND superseded_by IS NULL;",
            (tenant_id, invoice_id),
        )
        rec = cur.fetchone()

    def check(label: str, actual, expected) -> bool:
        ok = actual == expected
        print(f"  {'PASS' if ok else 'FAIL'}  {label:<34} {actual!r}"
              + ("" if ok else f"   (expected {expected!r})"))
        return ok

    print("\n" + "=" * 68)
    print(f"FRESH HERO IMPORT — {invoice_id}")
    print("=" * 68)
    print(f"  invoice status: {status}   elapsed: {_elapsed(started).strip()}")
    print("\n  worker attempts:")
    for task_type, attempt, state, err in attempts:
        print(f"    {task_type:<24} #{attempt} {state:<10} err={err}")

    print("\n  invariants:")
    ok = True
    ok &= check("pre-invoice events", n_events, 5)
    ok &= check("  all recorded before cutoff", n_before, n_events)
    ok &= check("day-event bindings written", n_bindings > 0, True)
    ok &= check("charged days", n_days, EXPECT_DAYS)
    ok &= check("total delta (minor)", int(sum_delta), EXPECT_DISPUTED_MINOR)
    ok &= check("delta per day (minor)",
                int(sum_delta) // n_days if n_days else 0,
                EXPECT_DELTA_PER_DAY_MINOR)
    if rec is None:
        print("  FAIL  recommendation                    None")
        ok = False
    else:
        version, rtype, rstate, claimed, supported, disputed = rec
        ok &= check("recommendation version", version, 1)
        ok &= check("recommendation type", rtype, "DISPUTE")
        ok &= check("recommendation state", rstate, "FROZEN")
        ok &= check("claimed (minor)", int(claimed), EXPECT_CLAIMED_MINOR)
        ok &= check("supported (minor)", int(supported), EXPECT_SUPPORTED_MINOR)
        ok &= check("disputed (minor)", int(disputed), EXPECT_DISPUTED_MINOR)

    print("=" * 68)
    print("RESULT:", "ALL INVARIANTS PASS — ready to record"
          if ok else "MISMATCH — do not record until reviewed")
    print("=" * 68)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
