"""Gate 5 live trace against tally_gate2_iso — human approval + atomic seal.

Seeds a frozen DISPUTE recommendation with seven day judgments, an applicable
rule, a reconstruction, an invoice, and an invoice source, then runs the real
approve_and_seal SERIALIZABLE transaction against live CockroachDB. Proves: the
seal binds every exact input (recommendation/reconstruction/rule/claim-set/
source/approver); a stale ETag is rejected; a repeated approval with the same
idempotency key replays the existing seal (no second seal). Uses
cluster_logical_timestamp() for the seal ts.

Writes only to a Gate-5 tenant in tally_gate2_iso. Never touches defaultdb.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timezone
from uuid import uuid4

import psycopg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.gate2_isolated_trace import _iso_dsn  # noqa: E402
from src.core.judgment import DayInput, resolve_recommendation  # noqa: E402
from src.external.dal import DAL, Tenant  # noqa: E402
from src.platform.authority_seal_repository import (  # noqa: E402
    StaleRecommendationError,
    approve_and_seal,
)

G5_TENANT = "10000000-0000-4000-8000-0000000000e6"
CARRIER = "20000000-0000-4000-8000-000000000030"
CUTOFF = datetime(2026, 6, 22, 8, 0, tzinfo=timezone.utc)
HERO_DATES = [date(2026, 6, d) for d in range(8, 15)]


def _seed(cur):
    invoice_id, recon_id, rule_id, rec_id = (str(uuid4()) for _ in range(4))
    cur.execute("INSERT INTO tenants (id,name) VALUES (%s,'Gate5 (fictional)') "
                "ON CONFLICT (id) DO NOTHING;", (G5_TENANT,))
    cur.execute("INSERT INTO carriers (tenant_id,id,scac,name) "
                "VALUES (%s,%s,'ASTL','Asterline (fictional)') "
                "ON CONFLICT DO NOTHING;", (G5_TENANT, CARRIER))
    cur.execute(
        """
        INSERT INTO invoices (tenant_id,id,carrier_id,invoice_no,received_at,s3_key,
            sha256,status,intake_state,aggregate_status,status_sequence,
            active_claim_set_version,row_version,display_name)
        VALUES (%s,%s,%s,'INV-1048',%s,'k',%s,'READY_FOR_REVIEW',
            'READY_FOR_RECONSTRUCTION','READY_FOR_REVIEW',5,1,2,'INV-1048.pdf');
        """,
        (G5_TENANT, invoice_id, CARRIER, CUTOFF, uuid4().hex),
    )
    cur.execute(
        """
        INSERT INTO invoice_sources (tenant_id,id,invoice_id,source_type,
            display_filename,mime_type,byte_length,sha256,s3_bucket_ref_private,
            s3_object_key_private,s3_version_id_private,preservation_status,
            provenance_classification,public_disclosure,verified_at,received_at)
        VALUES (%s,%s,%s,'INVOICE_PDF','INV-1048.pdf','application/pdf',1024,%s,
            'demo-bucket','intake/INV-1048.pdf','v1','VERSION_VERIFIED',
            'DEMO_SCENARIO','Representative demonstration data',%s,%s);
        """,
        (G5_TENANT, str(uuid4()), invoice_id, uuid4().hex, CUTOFF, CUTOFF),
    )
    task_id = str(uuid4())
    cur.execute(
        """
        INSERT INTO workflow_tasks (tenant_id,id,invoice_id,task_type,task_version,
            state,actor_display,knowledge_cutoff_at,input_fingerprint,
            input_object_refs,current_attempt)
        VALUES (%s,%s,%s,'JUDGE_DAYS',1,'COMPLETED','w',%s,%s,'[]',1);
        """,
        (G5_TENANT, task_id, invoice_id, CUTOFF, uuid4().hex),
    )
    cur.execute(
        """
        INSERT INTO reconstructions (tenant_id,id,invoice_id,version,task_id,
            input_fingerprint,claim_set_version,knowledge_cutoff_at,
            effective_timezone,state,event_count,days_total,days_complete,
            public_summary)
        VALUES (%s,%s,%s,1,%s,%s,1,%s,'America/Los_Angeles','COMPLETE',5,7,7,'ok');
        """,
        (G5_TENANT, recon_id, invoice_id, task_id, uuid4().hex, CUTOFF),
    )
    day_ids = []
    for d in HERO_DATES:
        did = str(uuid4())
        day_ids.append((did, d))
        cur.execute(
            """
            INSERT INTO reconstruction_charged_days (tenant_id,id,reconstruction_id,
                invoice_id,charge_date,invoice_claim_field,chargeability,
                coverage_state,state,invoice_rate_minor,applicable_rate_minor,
                currency,outcome,missing_requirements)
            VALUES (%s,%s,%s,%s,%s,'daily_rate','CHARGEABLE','PRESENT_VERIFIED',
                'SOURCE_COMPLETE',35000,25000,'USD','PENDING','[]');
            """,
            (G5_TENANT, did, recon_id, invoice_id, d),
        )
    cur.execute(
        """
        INSERT INTO rule_retrieval_runs (tenant_id,id,invoice_id,reconstruction_id,
            query_fingerprint,query_text_private,vector_index_name,embedding_model,
            embedding_input_sha256,state,candidate_count,completed_at)
        VALUES (%s,%s,%s,%s,%s,'q','idx','model',%s,'COMPLETED',1,now());
        """,
        (G5_TENANT, str(uuid4()), invoice_id, recon_id, uuid4().hex, uuid4().hex),
    )
    cur.execute("SELECT id FROM rule_retrieval_runs WHERE tenant_id=%s AND "
                "reconstruction_id=%s LIMIT 1;", (G5_TENANT, recon_id))
    run_id = cur.fetchone()[0]
    # Seed a minimal tariff snapshot + clause so the applicable_rule FK resolves.
    snapshot_id, clause_id = str(uuid4()), str(uuid4())
    cur.execute(
        """
        INSERT INTO tariff_snapshots (tenant_id,id,carrier_id,lane,version_label,
            effective_date,captured_at,source_url,s3_key,doc_sha256,doc_text,
            headline_rate,source_version_id)
        VALUES (%s,%s,%s,'USOAK','v1',%s,now(),'https://rep.example/t','rep/t',%s,
            'rep',250,'v1');
        """,
        (G5_TENANT, snapshot_id, CARRIER, date(2026, 6, 1), uuid4().hex),
    )
    cur.execute(
        """
        INSERT INTO tariff_clauses (tenant_id,id,carrier_id,snapshot_id,clause_ref,
            clause_kind,clause_text,rate_amount,sha256,embedding)
        VALUES (%s,%s,%s,%s,'Clause 4.2','rate','Demurrage $250 per calendar day',
            250.00,%s,%s::VECTOR);
        """,
        (G5_TENANT, clause_id, CARRIER, snapshot_id, uuid4().hex,
         json.dumps([0.0] * 1024)),
    )
    cur.execute(
        """
        INSERT INTO applicable_rules (tenant_id,id,invoice_id,reconstruction_id,
            retrieval_run_id,tariff_clause_id,public_ref,clause_ref,display_excerpt,
            rate_minor,currency,unit,effective_from,effective_to,scope_code,
            source_locator_private,source_version_state,validation_state,
            validation_results)
        VALUES (%s,%s,%s,%s,%s,%s,'RULE-1','Clause 4.2','Demurrage $250/day',25000,
            'USD','CALENDAR_DAY',%s,NULL,'DEMURRAGE:USOAK:DRY','s3://p','VERIFIED',
            'VERIFIED','{}');
        """,
        (G5_TENANT, rule_id, invoice_id, recon_id, run_id, clause_id,
         date(2026, 6, 1)),
    )

    # Freeze the recommendation with the real engine digest.
    days = [DayInput(d, 35000, 25000, "USD", "PRESENT_VERIFIED", True)
            for d in HERO_DATES]
    rec = resolve_recommendation(days)
    cur.execute(
        """
        INSERT INTO recommendations (tenant_id,id,invoice_id,reconstruction_id,
            applicable_rule_id,version,input_fingerprint,recommendation_type,
            disputed_amount_minor,supported_amount_minor,claimed_amount_minor,
            currency,days_total,days_covered,evidence_coverage,state,digest,
            public_summary)
        VALUES (%s,%s,%s,%s,%s,1,%s,%s,%s,%s,%s,'USD',7,7,'7 of 7 days','FROZEN',%s,%s);
        """,
        (G5_TENANT, rec_id, invoice_id, recon_id, rule_id, uuid4().hex,
         rec.recommendation_type.value, rec.disputed_amount_minor,
         rec.supported_amount_minor, rec.claimed_amount_minor, rec.digest,
         rec.summary),
    )
    for (did, d), j in zip(day_ids, rec.judgments):
        cur.execute(
            """
            INSERT INTO charged_day_judgments (tenant_id,id,invoice_id,
                reconstruction_id,recommendation_id,charged_day_id,charge_date,
                invoice_rate_minor,applicable_rate_minor,discrepancy_minor,currency,
                outcome,coverage_state,applicable_rule_id,explanation)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'USD',%s,'PRESENT_VERIFIED',%s,%s);
            """,
            (G5_TENANT, str(uuid4()), invoice_id, recon_id, rec_id, did, d,
             j.invoice_rate_minor, j.applicable_rate_minor, j.discrepancy_minor,
             j.outcome.value, rule_id, j.explanation),
        )
    return invoice_id, rec_id, rec.digest


def main() -> None:
    dsn = _iso_dsn()
    conn = psycopg.connect(dsn, connect_timeout=20, autocommit=True)
    with conn.cursor() as cur:
        for tbl in ["decision_seals", "approvals", "charged_day_judgments",
                    "recommendations", "applicable_rules", "rule_candidates",
                    "rule_retrieval_runs", "reconstruction_charged_days",
                    "reconstructions", "workflow_task_attempts", "workflow_tasks",
                    "invoice_sources", "event_outbox", "invoice_events", "invoices",
                    "tariff_clauses", "tariff_snapshots"]:
            cur.execute(f"DELETE FROM {tbl} WHERE tenant_id=%s;", (G5_TENANT,))
        invoice_id, rec_id, digest = _seed(cur)

    dal = DAL(conn, Tenant(G5_TENANT, "rachel.martinez"))
    # 1. Approve + seal the frozen recommendation.
    sealed = approve_and_seal(
        dal, recommendation_id=rec_id, expected_version=1, expected_digest=digest,
        idempotency_key="approve-1", approver_user_id=None,
        approver_display="rachel.martinez",
    )
    # 2. Idempotent replay — same key -> existing seal, no second seal.
    replay = approve_and_seal(
        dal, recommendation_id=rec_id, expected_version=1, expected_digest=digest,
        idempotency_key="approve-1", approver_user_id=None,
        approver_display="rachel.martinez",
    )
    # 3. Stale approval — wrong expected digest -> rejected.
    stale_rejected = False
    try:
        approve_and_seal(
            dal, recommendation_id=rec_id, expected_version=1,
            expected_digest="sha256:stale", idempotency_key="approve-2",
            approver_user_id=None, approver_display="rachel.martinez",
        )
    except StaleRecommendationError:
        stale_rejected = True

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM decision_seals WHERE tenant_id=%s;",
                    (G5_TENANT,))
        seal_count = cur.fetchone()[0]
        cur.execute("SELECT bound_object_refs, seal_digest, revision FROM "
                    "decision_seals WHERE tenant_id=%s;", (G5_TENANT,))
        refs, seal_dig, revision = cur.fetchone()
        cur.execute("SELECT count(*) FROM query_log WHERE tenant_id=%s AND tag='seal';",
                    (G5_TENANT,))
        audit_rows = cur.fetchone()[0]
        cur.execute("SELECT status FROM invoices WHERE tenant_id=%s AND id=%s;",
                    (G5_TENANT, invoice_id))
        invoice_status = cur.fetchone()[0]

    bound = refs if isinstance(refs, list) else json.loads(refs)
    trace = {
        "classification": "SYNTHETIC DEMO — FICTIONAL DATA",
        "database": "tally_gate2_iso (gate5 tenant; defaultdb untouched)",
        "sealed": {"revision": revision, "seal_digest": seal_dig,
                   "type": sealed.recommendation_type},
        "bound_input_types": sorted({r["type"] for r in bound}),
        "seal_count_after_replay": seal_count,
        "idempotent_replay_same_seal": replay.seal_id == sealed.seal_id,
        "stale_rejected": stale_rejected,
        "in_transaction_audit_rows": audit_rows,
        "invoice_status": invoice_status,
        "mock_fallback": False,
    }
    print(json.dumps(trace, indent=2))
    assert seal_count == 1, "exactly one seal despite replay"
    assert replay.seal_id == sealed.seal_id, "replay returns same seal"
    assert stale_rejected, "stale approval must be rejected"
    assert {"recommendation", "reconstruction", "claim_set", "applicable_rule"} \
        <= set(trace["bound_input_types"])
    assert invoice_status == "DISPUTED"
    conn.close()


if __name__ == "__main__":
    main()
