"""Restore INV-1048 to a known-good READY_FOR_REVIEW + FROZEN DISPUTE $700 state.

Use this when the hero is stuck (e.g. reconstruction crash-loop) and you need it
instantly approvable for filming, WITHOUT waiting for the live pipeline to re-run.
It rebuilds the reconstruction → charged days → applicable rule → FROZEN DISPUTE
recommendation chain directly (same shape gate5_isolated_trace / demo_v3_approval
use), keyed to the EXISTING hero invoice (keeps its invoice row + PDF + claims).

DISPUTE math: 7 days, invoice $350/day vs applicable $250/day → $100/day × 7 =
$700 disputed. The recommendation is frozen with the REAL engine digest
(resolve_recommendation), never hand-set — so the seal that follows binds a
genuine engine output.

Build-time demo housekeeping only (isolated judge lane). Idempotent: clears any
existing reconstruction/decision chain for the hero first, then reseeds.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from uuid import uuid4

import psycopg

from src.core.judgment import DayInput, RecommendationType, resolve_recommendation

HERO_DISPLAY_NAME = "INV-1048.pdf"
CUTOFF = datetime(2026, 6, 22, 8, 0, tzinfo=timezone.utc)
CHARGE_DATES = [date(2026, 6, d) for d in range(8, 15)]  # Jun 8..14 (7 days)
INVOICE_RATE_MINOR = 35000   # $350/day (carrier claim)
APPLICABLE_RATE_MINOR = 25000  # $250/day (tariff) → $100/day × 7 = $700 disputed
# The live pipeline sets shipment_ref FROM the container ref
# (reconstruction_repository._claim: shipment_ref=inputs["container_ref"]), and the
# MCP view stores it the same way. "SHP-1048" appeared nowhere in real memory.
HERO_SHIPMENT_REF = "TLLU4829317"
HERO_CONTAINER_REF = "TLLU4829317"

# The hero's sourced timeline. Each event was recorded well before the Jun-22
# knowledge cutoff (recorded_before_cutoff=true) — the core product point that the
# facts were on record before the invoice ever landed. occurred_at is the DATA
# domain (when it happened at the terminal); recorded_at is when memory saw it.
# public_refs, event->artifact links and shipment_ref MUST match
# tests/fixtures/demo/INV-1048.reconstruction-events.json, which is what
# seed_reconstruction_memory loads into shipment_event_memory (the table behind
# the Managed MCP view a LIVE reconstruction reads). Divergence here is invisible
# in the seeded hero and fatal on a live import — see _seed_events.
HERO_EVENTS = [
    ("SE-INV1048-001", "DISCHARGED", datetime(2026, 6, 2, 14, 12, tzinfo=timezone.utc),
     datetime(2026, 6, 2, 15, 0, tzinfo=timezone.utc),
     "milestone row: DISCHARGED 2026-06-02", "SRC-MILESTONE-INV-1048"),
    ("SE-INV1048-002", "AVAILABLE", datetime(2026, 6, 3, 8, 0, tzinfo=timezone.utc),
     datetime(2026, 6, 3, 9, 0, tzinfo=timezone.utc),
     "availability notice: AVAILABLE 2026-06-03", "SRC-AVAILABILITY-INV-1048"),
    ("SE-INV1048-003", "FREE_TIME_START",
     datetime(2026, 6, 3, 8, 0, tzinfo=timezone.utc),
     datetime(2026, 6, 3, 9, 0, tzinfo=timezone.utc),
     "free-time clock start: 2026-06-03", "SRC-AVAILABILITY-INV-1048"),
    ("SE-INV1048-004", "FREE_TIME_END",
     datetime(2026, 6, 7, 23, 59, tzinfo=timezone.utc),
     datetime(2026, 6, 8, 1, 0, tzinfo=timezone.utc),
     "free-time clock end: 2026-06-07", "SRC-AVAILABILITY-INV-1048"),
    ("SE-INV1048-005", "GATE_OUT", datetime(2026, 6, 14, 16, 30, tzinfo=timezone.utc),
     datetime(2026, 6, 14, 17, 0, tzinfo=timezone.utc),
     "milestone row: GATE_OUT 2026-06-14", "SRC-MILESTONE-INV-1048"),
]
HERO_SOURCE_ARTIFACTS = [
    {"public_ref": "SRC-MILESTONE-INV-1048", "source_type": "MILESTONE_EXPORT",
     "display_name": "Container milestone export (representative)",
     "adapter_name": "representative-milestone"},
    {"public_ref": "SRC-AVAILABILITY-INV-1048", "source_type": "AVAILABILITY_NOTICE",
     "display_name": "Terminal availability notice (representative)",
     "adapter_name": "representative-availability"},
]


def _hero_invoice(cur, tenant_id: str) -> tuple[str, str]:
    """(invoice_id, carrier_id) for the hero, from its existing invoice row."""
    cur.execute(
        "SELECT id, carrier_id FROM invoices WHERE tenant_id=%s AND display_name=%s;",
        (tenant_id, HERO_DISPLAY_NAME),
    )
    row = cur.fetchone()
    if row is None:
        raise SystemExit(f"{HERO_DISPLAY_NAME} not found for tenant {tenant_id}")
    return str(row[0]), str(row[1])


def _seed_events(cur, tenant_id: str, invoice_id: str, recon_id: str) -> None:
    """Insert the hero's sourced timeline into reconstruction_events.

    reconstruction_events.source_artifact_id is NOT NULL and FK-references
    reconstruction_source_artifacts, so the artifacts are seeded first and each
    event points at the one its fixture names.

    The artifact public_refs here MUST match the ones the real fixture's events
    reference (SRC-MILESTONE-INV-1048 / SRC-AVAILABILITY-INV-1048). An earlier
    version of this script invented a single 'SA-1048-MILESTONE' artifact, which
    made the seeded hero look fine while a LIVE import crash-looped: the live
    events reference the fixture refs, found no artifact, and their
    INSERT ... SELECT silently wrote zero rows -- surfacing later as a
    recon_day_binding_event_fk violation. Seeded and live paths now agree.

    We still deliberately DON'T write reconstruction_day_event_bindings — the
    charged days stand without bindings in this restore.
    """
    for artifact in HERO_SOURCE_ARTIFACTS:
        cur.execute(
            """
            INSERT INTO reconstruction_source_artifacts
                (tenant_id,id,invoice_id,public_ref,source_type,display_name,
                 mime_type,provenance_classification,public_disclosure,
                 adapter_name,s3_bucket_ref_private,s3_object_key_private,
                 s3_version_id_private,sha256,byte_length,verification_state,
                 recorded_at,verified_at)
            VALUES (%s,%s,%s,%s,%s,%s,
                'application/json','DEMO_SCENARIO',
                'Representative demonstration data',
                %s,'representative-demo-bucket',%s,
                'representative-v1',%s,0,'VERIFIED',%s,%s)
            ON CONFLICT (tenant_id, public_ref) DO NOTHING;
            """,
            (tenant_id, str(uuid4()), invoice_id, artifact["public_ref"],
             artifact["source_type"], artifact["display_name"],
             artifact["adapter_name"],
             f"representative/{artifact['public_ref']}",
             uuid4().hex, CUTOFF, CUTOFF),
        )
    for seq, (ref, etype, occurred, recorded, anchor, source_ref) in enumerate(
        HERO_EVENTS
    ):
        cur.execute(
            """
            INSERT INTO reconstruction_events
                (tenant_id,id,reconstruction_id,invoice_id,public_ref,event_type,
                 shipment_ref,container_ref,source_artifact_id,
                 source_version_ref_private,source_anchor_private,
                 display_anchor_public,provenance_classification,occurred_at,
                 recorded_at,received_at,recorded_before_cutoff,normalized_facts,
                 use_state,verification_state,display_sequence)
            SELECT %s,%s,%s,%s,%s,%s,%s,%s,a.id,a.s3_version_id_private,%s,%s,
                   'DEMO_SCENARIO',%s,%s,%s,true,'{}','USED','VERIFIED',%s
            FROM reconstruction_source_artifacts a
            WHERE a.tenant_id=%s AND a.public_ref=%s;
            """,
            (tenant_id, str(uuid4()), recon_id, invoice_id, ref, etype,
             HERO_SHIPMENT_REF, HERO_CONTAINER_REF,
             json.dumps({"anchor": anchor}), anchor, occurred, recorded, recorded,
             seq, tenant_id, source_ref),
        )


def _seed_dispute(cur, tenant_id: str, invoice_id: str, carrier_id: str) -> None:
    recon_id, rule_id, rec_id, task_id = (str(uuid4()) for _ in range(4))
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
                currency,outcome,dispute_amount_minor,missing_requirements)
            VALUES (%s,%s,%s,%s,%s,'daily_rate','CHARGEABLE','PRESENT_VERIFIED',
                'SOURCE_COMPLETE',%s,%s,'USD','RATE_DISCREPANCY',%s,'[]');
            """,
            (tenant_id, did, recon_id, invoice_id, d, INVOICE_RATE_MINOR,
             APPLICABLE_RATE_MINOR, INVOICE_RATE_MINOR - APPLICABLE_RATE_MINOR),
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
    snapshot_id, clause_id = str(uuid4()), str(uuid4())
    cur.execute(
        """
        INSERT INTO tariff_snapshots (tenant_id,id,carrier_id,lane,version_label,
            effective_date,captured_at,source_url,s3_key,doc_sha256,doc_text,
            headline_rate,source_version_id)
        VALUES (%s,%s,%s,'USOAK','v1',%s,now(),'https://rep.example/t1048','rep/t1048',
            %s,'rep',250,'v1');
        """,
        (tenant_id, snapshot_id, carrier_id, date(2026, 6, 1), uuid4().hex),
    )
    cur.execute(
        """
        INSERT INTO tariff_clauses (tenant_id,id,carrier_id,snapshot_id,clause_ref,
            clause_kind,clause_text,rate_amount,sha256,embedding)
        VALUES (%s,%s,%s,%s,'Clause 4.2','rate','Demurrage $250 per calendar day',
            250.00,%s,%s::VECTOR);
        """,
        (tenant_id, clause_id, carrier_id, snapshot_id, uuid4().hex,
         json.dumps([0.0] * 1024)),
    )
    cur.execute(
        """
        INSERT INTO applicable_rules (tenant_id,id,invoice_id,reconstruction_id,
            retrieval_run_id,tariff_clause_id,public_ref,clause_ref,display_excerpt,
            rate_minor,currency,unit,effective_from,effective_to,scope_code,
            source_locator_private,source_version_state,validation_state,
            validation_results)
        VALUES (%s,%s,%s,%s,%s,%s,'RULE-1048','Clause 4.2','Demurrage $250/day',%s,
            'USD','CALENDAR_DAY',%s,NULL,'DEMURRAGE:USOAK:DRY','s3://p','VERIFIED',
            'VERIFIED','{}');
        """,
        (tenant_id, rule_id, invoice_id, recon_id, run_id, clause_id,
         APPLICABLE_RATE_MINOR, date(2026, 6, 1)),
    )
    # Freeze the recommendation with the REAL engine — $350 vs $250 across 7 days
    # ⇒ DISPUTE $700.
    days = [DayInput(d, INVOICE_RATE_MINOR, APPLICABLE_RATE_MINOR, "USD",
                     "PRESENT_VERIFIED", True) for d in CHARGE_DATES]
    rec = resolve_recommendation(days)
    assert rec.recommendation_type is RecommendationType.DISPUTE, rec
    assert rec.disputed_amount_minor == 70000, rec
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
         f"{len(CHARGE_DATES)} of {len(CHARGE_DATES)} days", rec.digest, rec.summary),
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
    _seed_events(cur, tenant_id, invoice_id, recon_id)


def restore_hero(cur, tenant_id: str) -> dict:
    """Restore INV-1048 to READY_FOR_REVIEW using an EXISTING cursor.

    The single source of truth for the restore. `main()` wraps this for CLI use;
    the authenticated API route wraps it so a judge can rerun the scenario from
    the UI without shell access. Touches ONLY the hero — INV-1041 and INV-1047
    are never read or written here.

    Idempotent: _clear_derived removes any existing reconstruction/decision/send
    chain before reseeding, so repeated calls converge on the same state rather
    than duplicating rows.

    Returns the read-back facts so the caller can assert on them.
    """
    invoice_id, carrier_id = _hero_invoice(cur, tenant_id)
    _clear_derived(cur, tenant_id, invoice_id)
    _seed_dispute(cur, tenant_id, invoice_id, carrier_id)
    cur.execute(
        "UPDATE workflow_tasks SET state='COMPLETED',lease_owner=NULL,"
        "lease_expires_at=NULL,not_before=NULL WHERE tenant_id=%s AND invoice_id=%s "
        "AND task_type='START_RECONSTRUCTION';",
        (tenant_id, invoice_id),
    )
    cur.execute(
        "UPDATE invoices SET status='READY_FOR_REVIEW',"
        "aggregate_status='READY_FOR_REVIEW' WHERE tenant_id=%s AND id=%s;",
        (tenant_id, invoice_id),
    )
    cur.execute("SELECT aggregate_status FROM invoices WHERE tenant_id=%s AND id=%s;",
                (tenant_id, invoice_id))
    agg = cur.fetchone()[0]
    cur.execute("SELECT recommendation_type,state,disputed_amount_minor FROM "
                "recommendations WHERE tenant_id=%s AND invoice_id=%s AND "
                "superseded_by IS NULL;", (tenant_id, invoice_id))
    rec = cur.fetchone()
    cur.execute("SELECT count(*) FROM reconstruction_events WHERE tenant_id=%s "
                "AND invoice_id=%s;", (tenant_id, invoice_id))
    event_count = cur.fetchone()[0]
    return {
        "invoice_id": invoice_id,
        "aggregate_status": agg,
        "recommendation": rec,
        "event_count": event_count,
    }


def main() -> int:
    tenant_id = os.environ["TALLY_TENANT_ID"]
    conn = psycopg.connect(os.environ["TALLY_CRDB_DSN"], connect_timeout=20,
                           autocommit=True)
    with conn.cursor() as cur:
        result = restore_hero(cur, tenant_id)
    conn.close()
    assert result["aggregate_status"] == "READY_FOR_REVIEW", result
    assert result["recommendation"] == ("DISPUTE", "FROZEN", 70000), result
    assert result["event_count"] == len(HERO_EVENTS), result
    print(f"hero restored: {HERO_DISPLAY_NAME} → READY_FOR_REVIEW, "
          f"rec={result['recommendation']}, "
          f"timeline={result['event_count']} events "
          f"(instantly approvable, no pipeline wait)")
    return 0


def _clear_derived(cur, tenant_id: str, invoice_id: str) -> None:
    """Clear the hero's reconstruction/decision/send chain, keeping the invoice
    row + invoice_sources + claim_sets/extracted_claims. Reuses the FK-safe
    ordering from _demo_delete_invoice, minus the final invoice/source deletes."""
    # send + correspondence
    cur.execute("DELETE FROM send_gate_runs WHERE tenant_id=%s AND send_attempt_id IN "
                "(SELECT id FROM send_attempts WHERE tenant_id=%s AND invoice_id=%s);",
                (tenant_id, tenant_id, invoice_id))
    for tbl in ("send_attempts", "correspondence_drafts", "decision_seals",
                "approvals", "charged_day_judgments"):
        cur.execute(f"DELETE FROM {tbl} WHERE tenant_id=%s AND invoice_id=%s;",
                    (tenant_id, invoice_id))
    cur.execute("DELETE FROM charged_day_rule_bindings WHERE tenant_id=%s AND "
                "applicable_rule_id IN (SELECT id FROM applicable_rules WHERE "
                "tenant_id=%s AND invoice_id=%s);", (tenant_id, tenant_id, invoice_id))
    cur.execute("DELETE FROM applicable_rules WHERE tenant_id=%s AND invoice_id=%s;",
                (tenant_id, invoice_id))
    cur.execute("DELETE FROM rule_candidates WHERE tenant_id=%s AND retrieval_run_id IN "
                "(SELECT id FROM rule_retrieval_runs WHERE tenant_id=%s AND invoice_id=%s);",
                (tenant_id, tenant_id, invoice_id))
    cur.execute("DELETE FROM rule_retrieval_runs WHERE tenant_id=%s AND invoice_id=%s;",
                (tenant_id, invoice_id))
    cur.execute("DELETE FROM recommendations WHERE tenant_id=%s AND invoice_id=%s;",
                (tenant_id, invoice_id))
    cur.execute("DELETE FROM reconstruction_day_event_bindings WHERE tenant_id=%s AND "
                "charged_day_id IN (SELECT id FROM reconstruction_charged_days WHERE "
                "tenant_id=%s AND invoice_id=%s);", (tenant_id, tenant_id, invoice_id))
    for tbl in ("reconstruction_events", "reconstruction_source_artifacts",
                "reconstruction_coverage", "reconstruction_charged_days",
                "reconstructions"):
        cur.execute(f"DELETE FROM {tbl} WHERE tenant_id=%s AND invoice_id=%s;",
                    (tenant_id, invoice_id))


if __name__ == "__main__":
    raise SystemExit(main())
