"""Drive INV-1041 all the way to a genuine sealed historical approval.

INV-1041 is the clean queue row: a DISTINCT shipment (OOLU8401125 / OAK) whose
invoice daily rate EQUALS the applicable tariff rate, so the real judgment engine
(`resolve_recommendation`) returns APPROVE_FOR_PAYMENT with zero discrepancy. We
seed the invoice, its PDF source, the extracted claims, the reconstruction and
its six PRESENT_VERIFIED charged days, the applicable rule (matching clause), and
the FROZEN recommendation + day judgments — every amount computed by the real
engine, never hand-set — then run the REAL Gate-5 `approve_and_seal` SERIALIZABLE
transaction. The seal advances the invoice to APPROVED_FOR_PAYMENT (past tense),
so the queue reads it as a decision "done by a person previously".

Why seed the frozen recommendation directly instead of the live workers: the
deployed FIND_APPLICABLE_RULE worker is hard-wired to the $250 hero
(EXPECTED_RATE_PHRASE="$250", route USOAK); it cannot verify INV-1041's $90 clause
without touching the hero path. So the clean sealed history is produced exactly
the way gate5_isolated_trace produces the hero DISPUTE seal — real engine digest,
real seal transaction — only with equal rates so the recommendation is
APPROVE_FOR_PAYMENT.  Idempotent: re-running replays the existing seal.

Build-time only. Synthetic / DEMO_SCENARIO throughout. No hardcoded status,
amount, or reason lands anywhere — the outcome is the engine's + the seal's.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from uuid import uuid4

from src.core.judgment import DayInput, RecommendationType, resolve_recommendation
from src.external.dal import DAL, Tenant
from src.platform.authority_seal_repository import approve_and_seal

# Distinct identity from the hero (TLLU4829317) and the INV-1047 refusal
# (MSCU7011453). OAK terminal, 6 chargeable days, $90/day = $540, 0 discrepancy.
CARRIER = "20000000-0000-4000-8000-000000000041"
CUTOFF = datetime(2026, 5, 18, 8, 0, tzinfo=timezone.utc)
CHARGE_DATES = [date(2026, 5, d) for d in range(4, 10)]  # 2026-05-04 .. 05-09 (6)
DAILY_RATE_MINOR = 9000  # $90.00
TOTAL_MINOR = DAILY_RATE_MINOR * len(CHARGE_DATES)  # 54000 = $540.00
APPROVER_DISPLAY = "rachel.martinez"


def _seed_frozen_approval(cur, tenant_id: str) -> tuple[str, str, str]:
    """Seed the exact inputs a Gate-5 seal binds and freeze the recommendation
    with the REAL engine digest. Returns (invoice_id, recommendation_id, digest)."""
    invoice_id, recon_id, rule_id, rec_id = (str(uuid4()) for _ in range(4))
    cur.execute(
        "INSERT INTO carriers (tenant_id,id,scac,name) "
        "VALUES (%s,%s,'SBRT','Seabright (fictional)') ON CONFLICT DO NOTHING;",
        (tenant_id, CARRIER),
    )
    cur.execute(
        """
        INSERT INTO invoices (tenant_id,id,carrier_id,invoice_no,received_at,s3_key,
            sha256,status,intake_state,aggregate_status,status_sequence,
            active_claim_set_version,row_version,display_name)
        VALUES (%s,%s,%s,'INV-1041',%s,'intake/INV-1041.pdf',%s,'READY_FOR_REVIEW',
            'READY_FOR_RECONSTRUCTION','READY_FOR_REVIEW',5,1,2,'INV-1041.pdf');
        """,
        (tenant_id, invoice_id, CARRIER, CUTOFF, uuid4().hex),
    )
    cur.execute(
        """
        INSERT INTO invoice_sources (tenant_id,id,invoice_id,source_type,
            display_filename,mime_type,byte_length,sha256,s3_bucket_ref_private,
            s3_object_key_private,s3_version_id_private,preservation_status,
            provenance_classification,public_disclosure,verified_at,received_at)
        VALUES (%s,%s,%s,'INVOICE_PDF','INV-1041.pdf','application/pdf',1024,%s,
            'demo-bucket','intake/INV-1041.pdf','v1','VERSION_VERIFIED',
            'DEMO_SCENARIO','Representative demonstration data',%s,%s);
        """,
        (tenant_id, str(uuid4()), invoice_id, uuid4().hex, CUTOFF, CUTOFF),
    )
    # Claim set + the total claim so the queue amount column reads $540.
    claim_set_id = str(uuid4())
    cur.execute(
        """
        INSERT INTO claim_sets (tenant_id,id,invoice_id,claim_set_version,state)
        VALUES (%s,%s,%s,1,'ACTIVE') ON CONFLICT DO NOTHING;
        """,
        (tenant_id, claim_set_id, invoice_id),
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
        cur.execute(
            """
            INSERT INTO reconstruction_charged_days (tenant_id,id,reconstruction_id,
                invoice_id,charge_date,invoice_claim_field,chargeability,
                coverage_state,state,invoice_rate_minor,applicable_rate_minor,
                currency,outcome,missing_requirements)
            VALUES (%s,%s,%s,%s,%s,'daily_rate','CHARGEABLE','PRESENT_VERIFIED',
                'SOURCE_COMPLETE',%s,%s,'USD','PENDING','[]');
            """,
            (tenant_id, did, recon_id, invoice_id, d, DAILY_RATE_MINOR,
             DAILY_RATE_MINOR),
        )
    cur.execute(
        """
        INSERT INTO rule_retrieval_runs (tenant_id,id,invoice_id,reconstruction_id,
            query_fingerprint,query_text_private,vector_index_name,embedding_model,
            embedding_input_sha256,state,candidate_count,completed_at)
        VALUES (%s,%s,%s,%s,%s,'q','idx','model',%s,'COMPLETED',1,now());
        """,
        (tenant_id, str(uuid4()), invoice_id, recon_id, uuid4().hex, uuid4().hex),
    )
    cur.execute("SELECT id FROM rule_retrieval_runs WHERE tenant_id=%s AND "
                "reconstruction_id=%s LIMIT 1;", (tenant_id, recon_id))
    run_id = cur.fetchone()[0]
    # The matching tariff clause: rate EQUAL to the invoice daily rate ($90), so
    # the applicable rule verifies with zero discrepancy. Scoped OAK/DRY, distinct
    # from the INV-1047 refusal which is deliberately left with NO clause.
    snapshot_id, clause_id = str(uuid4()), str(uuid4())
    cur.execute(
        """
        INSERT INTO tariff_snapshots (tenant_id,id,carrier_id,lane,version_label,
            effective_date,captured_at,source_url,s3_key,doc_sha256,doc_text,
            headline_rate,source_version_id)
        VALUES (%s,%s,%s,'USOAK','v1',%s,now(),'https://rep.example/t1041','rep/t1041',
            %s,'rep',90,'v1');
        """,
        (tenant_id, snapshot_id, CARRIER, date(2026, 5, 1), uuid4().hex),
    )
    cur.execute(
        """
        INSERT INTO tariff_clauses (tenant_id,id,carrier_id,snapshot_id,clause_ref,
            clause_kind,clause_text,rate_amount,sha256,embedding)
        VALUES (%s,%s,%s,%s,'Clause 3.1','rate','Demurrage $90 per calendar day',
            90.00,%s,%s::VECTOR);
        """,
        (tenant_id, clause_id, CARRIER, snapshot_id, uuid4().hex,
         json.dumps([0.0] * 1024)),
    )
    cur.execute(
        """
        INSERT INTO applicable_rules (tenant_id,id,invoice_id,reconstruction_id,
            retrieval_run_id,tariff_clause_id,public_ref,clause_ref,display_excerpt,
            rate_minor,currency,unit,effective_from,effective_to,scope_code,
            source_locator_private,source_version_state,validation_state,
            validation_results)
        VALUES (%s,%s,%s,%s,%s,%s,'RULE-1041','Clause 3.1','Demurrage $90/day',%s,
            'USD','CALENDAR_DAY',%s,NULL,'DEMURRAGE:USOAK:DRY','s3://p','VERIFIED',
            'VERIFIED','{}');
        """,
        (tenant_id, rule_id, invoice_id, recon_id, run_id, clause_id,
         DAILY_RATE_MINOR, date(2026, 5, 1)),
    )
    # Freeze the recommendation with the REAL engine — equal rates ⇒ 0 discrepancy
    # ⇒ APPROVE_FOR_PAYMENT, supported == claimed == $540.
    days = [DayInput(d, DAILY_RATE_MINOR, DAILY_RATE_MINOR, "USD",
                     "PRESENT_VERIFIED", True) for d in CHARGE_DATES]
    rec = resolve_recommendation(days)
    assert rec.recommendation_type is RecommendationType.APPROVE_FOR_PAYMENT, rec
    assert rec.supported_amount_minor == TOTAL_MINOR, rec
    cur.execute(
        """
        INSERT INTO recommendations (tenant_id,id,invoice_id,reconstruction_id,
            applicable_rule_id,version,input_fingerprint,recommendation_type,
            disputed_amount_minor,supported_amount_minor,claimed_amount_minor,
            currency,days_total,days_covered,evidence_coverage,state,digest,
            public_summary)
        VALUES (%s,%s,%s,%s,%s,1,%s,%s,%s,%s,%s,'USD',%s,%s,%s,'FROZEN',%s,%s);
        """,
        (tenant_id, rec_id, invoice_id, recon_id, rule_id, uuid4().hex,
         rec.recommendation_type.value, rec.disputed_amount_minor,
         rec.supported_amount_minor, rec.claimed_amount_minor,
         len(CHARGE_DATES), len(CHARGE_DATES),
         f"{len(CHARGE_DATES)} of {len(CHARGE_DATES)} days", rec.digest,
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
            (tenant_id, str(uuid4()), invoice_id, recon_id, rec_id, did, d,
             j.invoice_rate_minor, j.applicable_rate_minor, j.discrepancy_minor,
             j.outcome.value, rule_id, j.explanation),
        )
    return invoice_id, rec_id, rec.digest


def drive_inv_1041_approval(conn, tenant_id: str) -> dict[str, object]:
    """Seed + REAL Gate-5 approve+seal INV-1041, then read back live state.

    Idempotent: an existing frozen recommendation is reused (we skip re-seeding),
    and the seal replays on its idempotency key.  Ends with read-back assertions
    that the LIVE projection state == intent (status APPROVED_FOR_PAYMENT, engine
    APPROVE_FOR_PAYMENT, supported $540) — never merely "the call succeeded".
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, digest FROM recommendations r "
            "JOIN invoices i ON i.tenant_id=r.tenant_id AND i.id=r.invoice_id "
            "WHERE r.tenant_id=%s AND i.invoice_no='INV-1041' "
            "AND r.superseded_by IS NULL ORDER BY r.version DESC LIMIT 1;",
            (tenant_id,),
        )
        existing = cur.fetchone()
        if existing is None:
            invoice_id, rec_id, digest = _seed_frozen_approval(cur, tenant_id)
        else:
            rec_id, digest = str(existing[0]), existing[1]
            cur.execute("SELECT invoice_id FROM recommendations WHERE tenant_id=%s "
                        "AND id=%s;", (tenant_id, rec_id))
            invoice_id = str(cur.fetchone()[0])

    dal = DAL(conn, Tenant(tenant_id, APPROVER_DISPLAY))
    sealed = approve_and_seal(
        dal, recommendation_id=rec_id, expected_version=1, expected_digest=digest,
        idempotency_key=f"inv-1041-approve-{rec_id}", approver_user_id=None,
        approver_display=APPROVER_DISPLAY,
    )

    # Read-back: assert LIVE state matches intent.
    with conn.cursor() as cur:
        cur.execute("SELECT status, aggregate_status FROM invoices "
                    "WHERE tenant_id=%s AND id=%s;", (tenant_id, invoice_id))
        status, aggregate = cur.fetchone()
        cur.execute("SELECT recommendation_type, supported_amount_minor, "
                    "claimed_amount_minor FROM recommendations "
                    "WHERE tenant_id=%s AND id=%s;", (tenant_id, rec_id))
        rec_type, supported, claimed = cur.fetchone()
        cur.execute("SELECT count(*) FROM decision_seals WHERE tenant_id=%s "
                    "AND invoice_id=%s;", (tenant_id, invoice_id))
        seal_count = cur.fetchone()[0]

    result = {
        "invoice_id": invoice_id,
        "status": status,
        "aggregate_status": aggregate,
        "recommendation_type": rec_type,
        "supported_amount_minor": int(supported),
        "claimed_amount_minor": int(claimed),
        "seal_count": seal_count,
        "seal_revision": sealed.revision,
    }
    assert rec_type == "APPROVE_FOR_PAYMENT", f"engine type {rec_type}"
    assert status == "APPROVED_FOR_PAYMENT", f"live status {status}"
    assert aggregate == "APPROVED_FOR_PAYMENT", f"live aggregate {aggregate}"
    assert int(supported) == TOTAL_MINOR, f"supported {supported} != {TOTAL_MINOR}"
    assert int(claimed) == TOTAL_MINOR, f"claimed {claimed} != {TOTAL_MINOR}"
    assert seal_count == 1, f"expected exactly one seal, got {seal_count}"
    return result
