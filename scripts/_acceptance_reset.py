"""Clean the isolated judge lane for a repeatable acceptance run: delete every
INV-1048 test invoice (FK-first, all lineage) and reset the June-11 snapshot to
PENDING, leaving the retained shipment_event_memory intact. Judge-lane only.

Finds invoices by the retained shipment (TLLU4829317) via their reconstruction /
claim rows, so it clears whatever prior run left behind without needing an id.
"""

import os
import psycopg

SHIPMENT = os.environ.get("TALLY_TEST_SHIPMENT", "TLLU4829317")

CHILD_THEN_PARENT = [
    "DELETE FROM reconstruction_day_event_bindings WHERE charged_day_id IN "
    "(SELECT id FROM reconstruction_charged_days WHERE invoice_id=%s);",
    "DELETE FROM charged_day_rule_bindings WHERE charged_day_id IN "
    "(SELECT id FROM reconstruction_charged_days WHERE invoice_id=%s);",
    "DELETE FROM charged_day_judgments WHERE invoice_id=%s;",
    "DELETE FROM reconstruction_charged_days WHERE invoice_id=%s;",
    "DELETE FROM reconstruction_events WHERE invoice_id=%s;",
    "DELETE FROM reconstruction_coverage WHERE invoice_id=%s;",
    "DELETE FROM decision_seals WHERE invoice_id=%s;",
    "DELETE FROM approvals WHERE invoice_id=%s;",
    "DELETE FROM recommendations WHERE invoice_id=%s;",
    "DELETE FROM applicable_rules WHERE invoice_id=%s;",
    "DELETE FROM rule_candidates WHERE retrieval_run_id IN "
    "(SELECT id FROM rule_retrieval_runs WHERE invoice_id=%s);",
    "DELETE FROM rule_retrieval_runs WHERE invoice_id=%s;",
    "DELETE FROM reconstruction_source_artifacts WHERE invoice_id=%s;",
    "DELETE FROM reconstructions WHERE invoice_id=%s;",
    "DELETE FROM workflow_task_attempts WHERE task_id IN "
    "(SELECT id FROM workflow_tasks WHERE invoice_id=%s);",
    "DELETE FROM workflow_tasks WHERE invoice_id=%s;",
    "DELETE FROM access_evidence_verifications WHERE invoice_id=%s;",
    "DELETE FROM workflow_retry_requests WHERE invoice_id=%s;",
    "DELETE FROM extracted_claims WHERE claim_set_id IN "
    "(SELECT id FROM claim_sets WHERE invoice_id=%s);",
    "DELETE FROM claim_sets WHERE invoice_id=%s;",
    "DELETE FROM extraction_runs WHERE invoice_id=%s;",
    "DELETE FROM invoice_sources WHERE invoice_id=%s;",
    "DELETE FROM event_outbox WHERE invoice_id=%s;",
    "DELETE FROM invoice_events WHERE invoice_id=%s;",
    "DELETE FROM invoices WHERE id=%s;",
]


def main() -> int:
    with psycopg.connect(os.environ["TALLY_CRDB_DSN"], connect_timeout=20) as c:
        with c.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT r.invoice_id FROM reconstructions r "
                "JOIN reconstruction_events e ON e.reconstruction_id=r.id "
                "WHERE e.shipment_ref=%s;",
                (SHIPMENT,),
            )
            ids = [row[0] for row in cur.fetchall()]
            for inv in ids:
                for sql in CHILD_THEN_PARENT:
                    cur.execute(sql, (inv,))
            cur.execute(
                "UPDATE shipment_event_memory SET source_version_state='PENDING' "
                "WHERE public_ref='SE-INV1048-AX-0611' "
                "AND source_version_state='VERIFIED';"
            )
            cur.execute(
                "SELECT source_version_state, count(*) FROM shipment_event_memory "
                "GROUP BY 1;"
            )
            memory = cur.fetchall()
        c.commit()
    print(f"  cleared {len(ids)} prior invoice(s); memory intact: {memory}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
