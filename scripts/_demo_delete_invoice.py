"""Delete an invoice + all its child rows from the demo tenant, in FK order.

Build-time demo housekeeping only (isolated judge lane). Not a runtime path.
Usage: python -m scripts._demo_delete_invoice <display_name> [<display_name> ...]
"""

from __future__ import annotations

import os
import sys

import psycopg

# Child tables that FK → invoices, plus the grandchild attempt/judgment tables,
# in delete-safe order (leaves first).
# Tables with a direct invoice_id column, in delete-safe (leaf-first) order.
_CHILD_BY_INVOICE = [
    "decision_seals",
    "approvals",
    "charged_day_judgments",
    "access_evidence_verifications",
    "recommendations",
    "applicable_rules",
    "rule_retrieval_runs",
    "reconstruction_charged_days",
    "reconstruction_events",
    "reconstruction_coverage",
    "reconstruction_source_artifacts",
    "reconstructions",
    "event_outbox",
    "invoice_events",
    "workflow_retry_requests",
    "extraction_runs",
    "invoice_sources",
]


def delete_invoice(cur, tenant_id: str, invoice_id: str) -> None:
    # workflow_task_attempts hangs off workflow_tasks (no direct invoice FK).
    cur.execute(
        "DELETE FROM workflow_task_attempts WHERE task_id IN "
        "(SELECT id FROM workflow_tasks WHERE tenant_id=%s AND invoice_id=%s);",
        (tenant_id, invoice_id),
    )
    # extracted_claims → claim_sets → extraction_runs: delete this chain in order
    # here (claim_sets before extraction_runs, which the generic loop deletes).
    cur.execute(
        "DELETE FROM extracted_claims WHERE tenant_id=%s AND claim_set_id IN "
        "(SELECT id FROM claim_sets WHERE tenant_id=%s AND invoice_id=%s);",
        (tenant_id, tenant_id, invoice_id),
    )
    cur.execute(
        "DELETE FROM claim_sets WHERE tenant_id=%s AND invoice_id=%s;",
        (tenant_id, invoice_id),
    )
    # charged_day_rule_bindings keys off applicable_rule_id (no invoice_id col).
    cur.execute(
        "DELETE FROM charged_day_rule_bindings WHERE tenant_id=%s AND applicable_rule_id IN "
        "(SELECT id FROM applicable_rules WHERE tenant_id=%s AND invoice_id=%s);",
        (tenant_id, tenant_id, invoice_id),
    )
    # rule_candidates keys off retrieval_run_id (no invoice_id col).
    cur.execute(
        "DELETE FROM rule_candidates WHERE tenant_id=%s AND retrieval_run_id IN "
        "(SELECT id FROM rule_retrieval_runs WHERE tenant_id=%s AND invoice_id=%s);",
        (tenant_id, tenant_id, invoice_id),
    )
    # reconstruction_day_event_bindings keys off charged_day_id (no invoice_id).
    cur.execute(
        "DELETE FROM reconstruction_day_event_bindings WHERE tenant_id=%s AND charged_day_id IN "
        "(SELECT id FROM reconstruction_charged_days WHERE tenant_id=%s AND invoice_id=%s);",
        (tenant_id, tenant_id, invoice_id),
    )
    for table in _CHILD_BY_INVOICE:
        cur.execute(
            f"DELETE FROM {table} WHERE tenant_id=%s AND invoice_id=%s;",
            (tenant_id, invoice_id),
        )
    cur.execute(
        "DELETE FROM workflow_tasks WHERE tenant_id=%s AND invoice_id=%s;",
        (tenant_id, invoice_id),
    )
    cur.execute(
        "DELETE FROM invoices WHERE tenant_id=%s AND id=%s;",
        (tenant_id, invoice_id),
    )


def main() -> int:
    tenant_id = os.environ["TALLY_TENANT_ID"]
    conn = psycopg.connect(os.environ["TALLY_CRDB_DSN"], autocommit=True)
    with conn.cursor() as cur:
        for name in sys.argv[1:]:
            cur.execute(
                "SELECT id FROM invoices WHERE tenant_id=%s AND display_name=%s;",
                (tenant_id, name),
            )
            rows = cur.fetchall()
            for (iid,) in rows:
                delete_invoice(cur, tenant_id, str(iid))
                print(f"deleted {name} ({iid})")
            if not rows:
                print(f"{name}: not present")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
