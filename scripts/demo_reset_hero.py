"""Reset the hero INV-1048 to BRAND-NEW (pre-reconstruction) so the deployed
workers re-run the whole pipeline (reconstruction -> judge -> recommend) on the
next open.

Keeps the invoice row, its invoice_sources (the retained PDF), and its
extracted_claims/claim_sets (the extracted 13-field claims). Deletes only the
DERIVED reconstruction/decision/send state, then resets the invoice + its
workflow_tasks back to the just-received-and-ready-to-reconstruct state:
- invoice: intake_state=READY_FOR_RECONSTRUCTION, aggregate_status=RECONSTRUCTING
  (exactly what intake leaves behind after it creates START_RECONSTRUCTION).
- START_RECONSTRUCTION workflow_task -> PENDING, lease/attempt/timestamps cleared
  so the deployed reconstruction worker re-leases it.
- every downstream workflow_task (FIND_APPLICABLE_RULE, etc.) + its attempts are
  deleted; the workers recreate them as they run.

Idempotent and safe to run repeatedly. Ends with a read-back of the hero's state.

Env: TALLY_CRDB_DSN, TALLY_TENANT_ID (the .sh wrapper reads them from SSM).
"""

from __future__ import annotations

import os
import sys

import psycopg

HERO_DISPLAY_NAME = os.environ.get("TALLY_HERO_DISPLAY_NAME", "INV-1048.pdf")

# Derived rows to delete, leaf-first (FK-safe). Mirrors _demo_delete_invoice.py's
# ordering minus the tables we KEEP (invoices, invoice_sources, claim_sets,
# extracted_claims, extraction_runs). Send tables lead (they FK the seal/draft).
_DELETE_BY_INVOICE = [
    "send_gate_runs",  # via send_attempt_id, handled specially below
    "send_attempts",
    "correspondence_drafts",
    "decision_seals",
    "approvals",
    "charged_day_judgments",
    "recommendations",
    "applicable_rules",
    "rule_retrieval_runs",  # rule_candidates/charged_day_rule_bindings handled below
    "reconstruction_events",
    "reconstruction_coverage",
    "reconstruction_charged_days",
    "reconstructions",
    "reconstruction_source_artifacts",
    "workflow_retry_requests",
    "event_outbox",
    "invoice_events",
]


def reset_hero(cur, tenant_id: str, invoice_id: str) -> None:
    # send_gate_runs keys off send_attempt_id (no invoice_id column).
    cur.execute(
        "DELETE FROM send_gate_runs WHERE tenant_id=%s AND send_attempt_id IN "
        "(SELECT id FROM send_attempts WHERE tenant_id=%s AND invoice_id=%s);",
        (tenant_id, tenant_id, invoice_id),
    )
    # charged_day_rule_bindings keys off applicable_rule_id (no invoice_id column).
    cur.execute(
        "DELETE FROM charged_day_rule_bindings WHERE tenant_id=%s AND applicable_rule_id IN "
        "(SELECT id FROM applicable_rules WHERE tenant_id=%s AND invoice_id=%s);",
        (tenant_id, tenant_id, invoice_id),
    )
    # rule_candidates keys off retrieval_run_id (no invoice_id column).
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
    for table in _DELETE_BY_INVOICE:
        if table == "send_gate_runs":
            continue  # already handled above (no invoice_id column)
        cur.execute(
            f"DELETE FROM {table} WHERE tenant_id=%s AND invoice_id=%s;",
            (tenant_id, invoice_id),
        )

    # Downstream workflow_tasks + their attempts (everything EXCEPT the one
    # START_RECONSTRUCTION row we reset to PENDING below). Attempts FK -> tasks.
    cur.execute(
        "DELETE FROM workflow_task_attempts WHERE tenant_id=%s AND task_id IN "
        "(SELECT id FROM workflow_tasks WHERE tenant_id=%s AND invoice_id=%s "
        " AND task_type <> 'START_RECONSTRUCTION');",
        (tenant_id, tenant_id, invoice_id),
    )
    cur.execute(
        "DELETE FROM workflow_tasks WHERE tenant_id=%s AND invoice_id=%s "
        "AND task_type <> 'START_RECONSTRUCTION';",
        (tenant_id, invoice_id),
    )
    # Also clear the attempts on the START_RECONSTRUCTION task itself so its
    # re-run starts from attempt 0 with no stale lease/attempt history.
    cur.execute(
        "DELETE FROM workflow_task_attempts WHERE tenant_id=%s AND task_id IN "
        "(SELECT id FROM workflow_tasks WHERE tenant_id=%s AND invoice_id=%s "
        " AND task_type = 'START_RECONSTRUCTION');",
        (tenant_id, tenant_id, invoice_id),
    )
    # Reset START_RECONSTRUCTION to a fresh PENDING lease so the deployed worker
    # re-leases and re-runs it (re-emitting FIND_APPLICABLE_RULE downstream).
    cur.execute(
        """
        UPDATE workflow_tasks
        SET state='PENDING', current_attempt=0, lease_owner=NULL,
            lease_expires_at=NULL, not_before=NULL, started_at=NULL,
            completed_at=NULL, private_error_code=NULL, private_error_ref=NULL,
            public_summary='Waiting to reconstruct the charged period',
            updated_at=now()
        WHERE tenant_id=%s AND invoice_id=%s AND task_type='START_RECONSTRUCTION';
        """,
        (tenant_id, invoice_id),
    )
    # Invoice back to the just-received-and-ready-to-reconstruct state (exactly
    # what intake leaves behind once it has created the START_RECONSTRUCTION task).
    cur.execute(
        """
        UPDATE invoices
        SET intake_state='READY_FOR_RECONSTRUCTION',
            aggregate_status='RECONSTRUCTING', status='RECONSTRUCTING'
        WHERE tenant_id=%s AND id=%s;
        """,
        (tenant_id, invoice_id),
    )


def _read_back(cur, tenant_id: str, invoice_id: str) -> dict:
    cur.execute(
        "SELECT intake_state, aggregate_status FROM invoices WHERE tenant_id=%s AND id=%s;",
        (tenant_id, invoice_id),
    )
    inv = cur.fetchone()
    cur.execute(
        "SELECT task_type, state FROM workflow_tasks WHERE tenant_id=%s AND invoice_id=%s "
        "ORDER BY task_type;",
        (tenant_id, invoice_id),
    )
    tasks = cur.fetchall()
    counts = {}
    for tbl in ("reconstructions", "applicable_rules", "recommendations",
                "decision_seals", "correspondence_drafts", "send_attempts"):
        cur.execute(
            f"SELECT count(*) FROM {tbl} WHERE tenant_id=%s AND invoice_id=%s;",
            (tenant_id, invoice_id),
        )
        counts[tbl] = cur.fetchone()[0]
    cur.execute(
        "SELECT count(*) FROM claim_sets WHERE tenant_id=%s AND invoice_id=%s;",
        (tenant_id, invoice_id),
    )
    claim_sets = cur.fetchone()[0]
    cur.execute(
        "SELECT count(*) FROM invoice_sources WHERE tenant_id=%s AND invoice_id=%s;",
        (tenant_id, invoice_id),
    )
    sources = cur.fetchone()[0]
    return {"invoice": inv, "tasks": tasks, "derived_counts": counts,
            "kept_claim_sets": claim_sets, "kept_invoice_sources": sources}


def main() -> int:
    tenant_id = os.environ["TALLY_TENANT_ID"]
    dsn = os.environ["TALLY_CRDB_DSN"]
    conn = psycopg.connect(dsn, connect_timeout=20)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM invoices WHERE tenant_id=%s AND display_name=%s;",
            (tenant_id, HERO_DISPLAY_NAME),
        )
        rows = cur.fetchall()
        if not rows:
            print(f"hero {HERO_DISPLAY_NAME!r} not present for tenant {tenant_id}; "
                  "nothing to reset.")
            conn.close()
            return 1
        for (iid,) in rows:
            invoice_id = str(iid)
            reset_hero(cur, tenant_id, invoice_id)
            state = _read_back(cur, tenant_id, invoice_id)
            print(f"== reset {HERO_DISPLAY_NAME} ({invoice_id}) ==")
            print(f"  invoice (intake_state, aggregate_status): {state['invoice']}")
            print(f"  workflow_tasks: {state['tasks']}")
            print(f"  derived rows (all should be 0): {state['derived_counts']}")
            print(f"  KEPT claim_sets={state['kept_claim_sets']} "
                  f"invoice_sources={state['kept_invoice_sources']}")
    conn.commit()
    conn.close()
    print("hero reset to fresh; next open re-runs the pipeline "
          "(reconstruction -> judge -> recommend).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
