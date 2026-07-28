"""Seed INV-1047 as a genuine NEEDS EVIDENCE refusal row.

INV-1047 ($875, 7 × $125) has complete operational history but NO governing
tariff. On the deployed lane the FIND_APPLICABLE_RULE worker is hard-wired to the
hero ($250/USOAK/DRY) and would wrongly APPROVE it, so — exactly like INV-1041 is
sealed directly rather than run through that hero-bound worker — we seed the
refusal deterministically: charged days with NO applicable rate → the REAL engine
(`resolve_recommendation`) returns REQUEST_EVIDENCE + RULE_NOT_VERIFIED, and the
invoice aggregate is set to NEEDS_EVIDENCE. No seal (a refusal authorizes no
financial action). The reason "Governing tariff not verified" is the projection's
rendering of RULE_NOT_VERIFIED — not a hardcoded status.

Build-time only. Synthetic / DEMO_SCENARIO. No status/amount hardcoded; the
disposition is the engine's.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from uuid import uuid4

from src.core.judgment import (
    DayInput,
    ReasonCode,
    RecommendationType,
    resolve_recommendation,
)

CARRIER = "20000000-0000-4000-8000-000000000047"
CONTAINER = "MSCU-701145-3"  # matches the INV-1047 fixture; distinct shipment
CUTOFF = datetime(2026, 6, 24, 8, 0, tzinfo=timezone.utc)
CHARGE_DATES = [date(2026, 6, d) for d in range(8, 15)]  # Jun 8..14 (7)
DAILY_RATE_MINOR = 12500  # $125.00 (carrier claim)
TOTAL_MINOR = DAILY_RATE_MINOR * len(CHARGE_DATES)  # 87500 = $875.00


def _seed_refusal(cur, tenant_id: str) -> str:
    """Seed INV-1047 → genuine REQUEST_EVIDENCE (no verified rule) → NEEDS_EVIDENCE.
    Returns the invoice_id. Idempotent by display_name."""
    cur.execute(
        "SELECT id FROM invoices WHERE tenant_id=%s AND display_name='INV-1047.pdf' "
        "LIMIT 1;",
        (tenant_id,),
    )
    row = cur.fetchone()
    if row is not None:
        return str(row[0])

    invoice_id, recon_id, rec_id = (str(uuid4()) for _ in range(3))
    cur.execute(
        "INSERT INTO carriers (tenant_id,id,scac,name) "
        "VALUES (%s,%s,'MSCU','Meridian Shipping (fictional)') ON CONFLICT DO NOTHING;",
        (tenant_id, CARRIER),
    )
    cur.execute(
        """
        INSERT INTO invoices (tenant_id,id,carrier_id,invoice_no,received_at,s3_key,
            sha256,status,intake_state,aggregate_status,status_sequence,
            active_claim_set_version,row_version,display_name)
        VALUES (%s,%s,%s,'INV-1047',%s,'intake/INV-1047.pdf',%s,'NEEDS_EVIDENCE',
            'READY_FOR_RECONSTRUCTION','NEEDS_EVIDENCE',6,1,2,'INV-1047.pdf');
        """,
        (tenant_id, invoice_id, CARRIER, CUTOFF, uuid4().hex),
    )
    source_id = str(uuid4())
    cur.execute(
        """
        INSERT INTO invoice_sources (tenant_id,id,invoice_id,source_type,
            display_filename,mime_type,byte_length,sha256,s3_bucket_ref_private,
            s3_object_key_private,s3_version_id_private,preservation_status,
            provenance_classification,public_disclosure,verified_at,received_at)
        VALUES (%s,%s,%s,'INVOICE_PDF','INV-1047.pdf','application/pdf',1024,%s,
            'demo-bucket','intake/INV-1047.pdf','v1','VERSION_VERIFIED',
            'DEMO_SCENARIO','Representative demonstration data',%s,%s);
        """,
        (tenant_id, source_id, invoice_id, uuid4().hex, CUTOFF, CUTOFF),
    )
    extraction_run_id, claim_set_id = str(uuid4()), str(uuid4())
    cur.execute(
        """
        INSERT INTO extraction_runs (tenant_id,id,invoice_id,source_id,
            source_sha256,source_version_ref_private,model_id,schema_version,
            template_version,attempt,requested_at,validation_state)
        VALUES (%s,%s,%s,%s,%s,'v1','representative',1,1,1,%s,'VERIFIED');
        """,
        (tenant_id, extraction_run_id, invoice_id, source_id, uuid4().hex, CUTOFF),
    )
    cur.execute(
        """
        INSERT INTO claim_sets (tenant_id,id,invoice_id,claim_set_version,
            extraction_run_id,validation_state)
        VALUES (%s,%s,%s,1,%s,'VERIFIED') ON CONFLICT DO NOTHING;
        """,
        (tenant_id, claim_set_id, invoice_id, extraction_run_id),
    )
    for field, minor in (("total", TOTAL_MINOR), ("daily_rate", DAILY_RATE_MINOR)):
        cur.execute(
            """
            INSERT INTO extracted_claims (tenant_id,id,claim_set_id,field_name,
                value_type,normalized_value,amount_minor,currency,validation_state)
            VALUES (%s,%s,%s,%s,'MONEY',%s,%s,'USD','VERIFIED');
            """,
            (tenant_id, str(uuid4()), claim_set_id, field,
             json.dumps(f"{minor / 100:.2f}"), minor),
        )
    # Container claim so the queue/detail row shows an identifier (STRING claim).
    cur.execute(
        """
        INSERT INTO extracted_claims (tenant_id,id,claim_set_id,field_name,
            value_type,normalized_value,currency,validation_state)
        VALUES (%s,%s,%s,'container_number','STRING',%s,NULL,'VERIFIED');
        """,
        (tenant_id, str(uuid4()), claim_set_id, json.dumps(CONTAINER)),
    )
    task_id = str(uuid4())
    cur.execute(
        """
        INSERT INTO workflow_tasks (tenant_id,id,invoice_id,task_type,task_version,
            state,actor_display,knowledge_cutoff_at,input_fingerprint,
            input_object_refs,current_attempt)
        VALUES (%s,%s,%s,'JUDGE_DAYS',1,'COMPLETED','w',%s,%s,'[]',1);
        """,
        (tenant_id, task_id, invoice_id, CUTOFF, uuid4().hex),
    )
    cur.execute(
        """
        INSERT INTO reconstructions (tenant_id,id,invoice_id,version,task_id,
            input_fingerprint,claim_set_version,knowledge_cutoff_at,
            effective_timezone,state,event_count,days_total,days_complete,
            public_summary)
        VALUES (%s,%s,%s,1,%s,%s,1,%s,'America/Los_Angeles','COMPLETE',5,%s,%s,'ok');
        """,
        (tenant_id, recon_id, invoice_id, task_id, uuid4().hex, CUTOFF,
         len(CHARGE_DATES), len(CHARGE_DATES)),
    )
    day_ids = []
    for d in CHARGE_DATES:
        did = str(uuid4())
        day_ids.append((did, d))
        # PRESENT_VERIFIED coverage (history is complete) but NO applicable rate:
        # the governing tariff is absent, so judgment resolves INSUFFICIENT_EVIDENCE.
        cur.execute(
            """
            INSERT INTO reconstruction_charged_days (tenant_id,id,reconstruction_id,
                invoice_id,charge_date,invoice_claim_field,chargeability,
                coverage_state,state,invoice_rate_minor,applicable_rate_minor,
                currency,outcome,missing_requirements)
            VALUES (%s,%s,%s,%s,%s,'daily_rate','CHARGEABLE','PRESENT_VERIFIED',
                'SOURCE_COMPLETE',%s,NULL,'USD','PENDING','[]');
            """,
            (tenant_id, did, recon_id, invoice_id, d, DAILY_RATE_MINOR),
        )

    # Real engine: chargeable days with NO applicable rate → INSUFFICIENT_EVIDENCE
    # → REQUEST_EVIDENCE + RULE_NOT_VERIFIED. Never hand-set.
    days = [DayInput(d, DAILY_RATE_MINOR, None, "USD", "PRESENT_VERIFIED", True)
            for d in CHARGE_DATES]
    rec = resolve_recommendation(days)
    assert rec.recommendation_type is RecommendationType.REQUEST_EVIDENCE, rec
    assert ReasonCode.RULE_NOT_VERIFIED in rec.reason_codes, rec.reason_codes
    cur.execute(
        """
        INSERT INTO recommendations (tenant_id,id,invoice_id,reconstruction_id,
            applicable_rule_id,version,input_fingerprint,recommendation_type,
            disputed_amount_minor,supported_amount_minor,claimed_amount_minor,
            currency,days_total,days_covered,evidence_coverage,state,digest,
            public_summary,reason_codes)
        VALUES (%s,%s,%s,%s,NULL,1,%s,%s,%s,%s,%s,'USD',%s,%s,%s,'FROZEN',%s,%s,%s);
        """,
        (tenant_id, rec_id, invoice_id, recon_id, uuid4().hex,
         rec.recommendation_type.value, rec.disputed_amount_minor,
         rec.supported_amount_minor, rec.claimed_amount_minor,
         rec.days_total, rec.days_covered, rec.evidence_coverage, rec.digest,
         rec.summary, json.dumps([c.value for c in rec.reason_codes])),
    )
    for (did, d), j in zip(day_ids, rec.judgments):
        cur.execute(
            """
            INSERT INTO charged_day_judgments (tenant_id,id,invoice_id,
                reconstruction_id,recommendation_id,charged_day_id,charge_date,
                invoice_rate_minor,applicable_rate_minor,discrepancy_minor,currency,
                outcome,coverage_state,applicable_rule_id,explanation)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NULL,%s,'USD',%s,'PRESENT_VERIFIED',
                NULL,%s);
            """,
            (tenant_id, str(uuid4()), invoice_id, recon_id, rec_id, did, d,
             j.invoice_rate_minor, j.discrepancy_minor, j.outcome.value,
             j.explanation),
        )
    return invoice_id


def drive_inv_1047_refusal(conn, tenant_id: str) -> dict[str, object]:
    """Seed INV-1047 NEEDS EVIDENCE refusal, then read back live state."""
    with conn.cursor() as cur:
        invoice_id = _seed_refusal(cur, tenant_id)
        cur.execute(
            "SELECT status, aggregate_status FROM invoices WHERE tenant_id=%s "
            "AND id=%s;",
            (tenant_id, invoice_id),
        )
        status, aggregate = cur.fetchone()
        cur.execute(
            "SELECT recommendation_type, reason_codes, claimed_amount_minor "
            "FROM recommendations WHERE tenant_id=%s AND invoice_id=%s "
            "AND superseded_by IS NULL;",
            (tenant_id, invoice_id),
        )
        rec_type, reason_codes, claimed = cur.fetchone()

    result = {
        "invoice_id": invoice_id,
        "status": status,
        "aggregate_status": aggregate,
        "recommendation_type": rec_type,
        "reason_codes": reason_codes,
        "claimed_amount_minor": int(claimed),
    }
    assert aggregate == "NEEDS_EVIDENCE", f"live aggregate {aggregate}"
    assert rec_type == "REQUEST_EVIDENCE", f"engine type {rec_type}"
    assert int(claimed) == TOTAL_MINOR, f"claimed {claimed} != {TOTAL_MINOR}"
    return result
